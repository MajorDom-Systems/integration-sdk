"""The ``ble/router`` contract: one Bluetooth LE router that hears every radio and reaches every device.

Integrations need it as `BLE_ROUTER` and get it at ``self.peripherals.instance(BLE_ROUTER)``; never raw
Bluetooth, which the router holds. Its sources are the radios the runtime binds to it (``ble/bluez`` adapters,
``ble/silabs-cpc-hci``, ``ble/esphome-proxy`` nodes an integration brings along): `BLESource`s.

Listeners register `BLEMatcher`s and hear about matching devices whichever source saw them.
`BLERouter.connect` opens a GATT connection through the source that hears the device best, closed when its block
ends; `BLERouter.exclusive` leases that one source to a device for as long as pairing it takes. Whatever waits takes a
``busy_timeout``, and connections and leases end after ``max_duration``.
Credentials go to a device only over a link a `SecureChannel` (`ble_security`) has secured first.

bleak's advertisement types describe what is heard; this module imports them for type checking only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from majordom_integration_sdk.peripherals.need import Need

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

type OnAdvertisement = Callable[[BLEDevice, AdvertisementData], None]

CONNECT_BUSY_TIMEOUT = 10.0  # seconds to reach a device, a radio busy with another lease included
LEASE_BUSY_TIMEOUT = 30.0  # seconds to wait for a free radio to lease
LEASE_MAX_DURATION = 120.0  # seconds a lease holds its radio before it is taken back


@dataclass(frozen=True)
class BLEMatcher:
    """What a listener wants to hear about; every field that is set must match. A manufacturer id alone selects
    protocols that advertise no service UUID, e.g. HomeKit (HAP-BLE) accessories by Apple's company id."""

    service_uuid: UUID | None = None  # advertised as a service or in the service data
    service_data_uuid: UUID | None = None  # in the service data only (what the listener reads)
    manufacturer_id: int | None = None
    manufacturer_data_start: bytes = b""
    local_name_prefix: str | None = None

    def matches(self, adv: AdvertisementData) -> bool:
        if self.service_uuid and str(self.service_uuid) not in {*adv.service_uuids, *adv.service_data}:
            return False
        if self.service_data_uuid and str(self.service_data_uuid) not in adv.service_data:
            return False
        if self.manufacturer_id is not None:
            data = adv.manufacturer_data.get(self.manufacturer_id)
            if data is None or not bytes(data).startswith(self.manufacturer_data_start):
                return False
        return not self.local_name_prefix or (adv.local_name or "").startswith(self.local_name_prefix)


@dataclass
class BLEDiscoveryInfo:
    device: BLEDevice
    advertisement: AdvertisementData
    source: str = "local"  # the source that hears it best: "hci0", "esphome:<mac>", …
    rssi: int | None = None  # as that source hears it
    last_seen: float = 0.0  # monotonic


class BLEDiscoveryListener(Protocol):
    async def ble_did_discover_device(self, ble: BLERouter, info: BLEDiscoveryInfo): ...
    async def ble_did_update_device(self, ble: BLERouter, info: BLEDiscoveryInfo): ...
    async def ble_did_remove_device(self, ble: BLERouter, info: BLEDiscoveryInfo): ...


@dataclass(frozen=True)
class GattCharacteristic:
    uuid: str
    handle: int
    properties: tuple[str, ...]  # "read", "write", "write-without-response", "notify", "indicate", …


@dataclass(frozen=True)
class GattService:
    uuid: str
    handle: int
    characteristics: tuple[GattCharacteristic, ...]


class BLEConnection(Protocol):
    """An open GATT connection; characteristics are addressed by handle."""

    @property
    def mtu(self) -> int: ...
    async def services(self) -> list[GattService]: ...
    async def read(self, handle: int) -> bytes: ...
    async def write(self, handle: int, data: bytes, is_response_needed: bool = True): ...
    async def start_notify(self, handle: int, callback: Callable[[bytes], None]): ...
    async def stop_notify(self, handle: int): ...
    async def disconnect(self): ...


@runtime_checkable
class PairableBLEConnection(BLEConnection, Protocol):
    """A connection that can encrypt its link by pairing (LE Secure Connections where both ends support it)."""

    async def pair(self):
        """Pairs "Just Works", encrypting the link. Raises `BLEPairingError`."""
        ...

    async def unpair(self):
        """Forgets the bond; the link may drop."""
        ...


class BLESource(Protocol):
    """A radio that hears advertisements and can connect to devices: a local adapter or a proxy. Optional
    attributes the router reads: a true ``is_pairable`` when its connections are `PairableBLEConnection`s,
    ``free_slots``, the connections it can still open (a proxy has a few), None when it can't tell, and a true
    ``is_loss_reported`` when its radio going away is reported otherwise (an ESPHome node's API connection drops, and
    the integration takes the source away): the router then never restarts it for hearing nothing for a while."""

    name: str

    async def start(self, on_advertisement: OnAdvertisement): ...
    async def stop(self): ...
    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection: ...


@runtime_checkable
class PausableBLESource(BLESource, Protocol):
    """A source that can stop hearing without letting go of its radio, e.g. a proxy whose connections only
    work while it stays subscribed; a lease pauses one rather than stopping it."""

    async def pause_scanning(self): ...
    async def resume_scanning(self): ...


@runtime_checkable
class ScanModeBLESource(BLESource, Protocol):
    """A source that scans passively (every advertiser, the listeners' matchers choose) or actively (asking for the
    scan responses that often carry the name): the router switches it for a pairing window. Called before `start`
    too; a running source applies a change itself."""

    async def set_scan_mode(self, is_active: bool): ...


class BLEBusyError(ConnectionError):
    """Every source that hears the device is leased to someone else."""


class BLEPairingError(ConnectionError):
    """The link could not be encrypted: pairing failed, or no source that hears the device can pair."""


@dataclass(frozen=True)
class BLELease:
    """One source held for one device: it doesn't scan, and only the holder connects through it."""

    address: str
    source: str
    owner: str
    _source: BLESource = field(repr=False)

    def connect(
        self,
        on_disconnect: Callable[[], None] = lambda: None,
        *,
        busy_timeout: float = CONNECT_BUSY_TIMEOUT,
        max_duration: float | None = None,
    ) -> contextlib.AbstractAsyncContextManager[BLEConnection]:
        """A GATT connection through the leased source, closed when the block ends."""
        return connected(lambda: self._source.connect(self.address, on_disconnect), busy_timeout, max_duration)

    async def open_connection(
        self,
        on_disconnect: Callable[[], None] = lambda: None,
        *,
        busy_timeout: float = CONNECT_BUSY_TIMEOUT,
        max_duration: float | None = None,
    ) -> BLEConnection:
        """Like `connect`, without the block: disconnect it yourself."""
        return await open_connection(
            lambda: self._source.connect(self.address, on_disconnect), busy_timeout, max_duration
        )


class BLERouter(Protocol):
    """Bluetooth LE across all sources: discovery for listeners, GATT connections on demand."""

    def register(self, listener: BLEDiscoveryListener, matchers: Iterable[BLEMatcher | UUID]) -> Callable[[], None]:
        """Registers a listener for BLE devices matching any of ``matchers`` (a UUID matches that service).
        `ble_did_discover_device` is called when a device first matches, `ble_did_update_device` when its
        advertisement changes, `ble_did_remove_device` when no source has heard it for a while; devices heard
        before it registered are new to it. Returns a cancel function — call it on stop to deregister."""
        ...

    def connect(
        self,
        address: str,
        on_disconnect: Callable[[], None] = lambda: None,
        *,
        busy_timeout: float = CONNECT_BUSY_TIMEOUT,
        max_duration: float | None = None,
    ) -> contextlib.AbstractAsyncContextManager[BLEConnection]:
        """A GATT connection to ``address`` through the source hearing it best, falling back to the others, closed
        when the block ends. Leased sources are waited for up to ``busy_timeout`` seconds, then `BLEBusyError`;
        `ConnectionError` when no source reaches the device in that time. With ``max_duration``, it is closed after
        that many seconds. ``on_disconnect`` is called when the connection drops."""
        ...

    async def open_connection(
        self,
        address: str,
        on_disconnect: Callable[[], None] = lambda: None,
        *,
        busy_timeout: float = CONNECT_BUSY_TIMEOUT,
        max_duration: float | None = None,
    ) -> BLEConnection:
        """Like `connect`, without the block: disconnect it yourself. Only when the connection must outlive a
        block, e.g. a subscription kept for the integration's lifetime."""
        ...

    def exclusive(
        self,
        address: str,
        *,
        owner: str,
        busy_timeout: float = LEASE_BUSY_TIMEOUT,
        max_duration: float = LEASE_MAX_DURATION,
        wait: float = 1,
        is_pairing_needed: bool = False,
    ) -> contextlib.AbstractAsyncContextManager[BLELease]:
        """Leases the source hearing ``address`` best to ``owner`` (e.g. while pairing it): that source pauses
        scanning and others' connects avoid it; the rest keep going. A leased source is waited for ``wait`` seconds
        before the next best is tried, round after round, until ``busy_timeout`` raises `BLEBusyError`. The lease
        ends after ``max_duration`` seconds: the block gets `TimeoutError`, and the radio goes back. With
        ``is_pairing_needed``, only sources that can pair are leased (`BLEPairingError` when none hears it)."""
        ...

    async def scan_actively(self, duration: float):
        """Scans actively for ``duration`` seconds (a pairing window): names often come only in the scan
        responses a passive scan never asks for."""
        ...

    def devices(self) -> list[BLEDiscoveryInfo]:
        """Every device heard recently, as the source hearing it best sees it."""
        ...


BLE_ROUTER = Need[BLERouter]("ble", ["router"])  # optional: BLE_ROUTER(is_required=False)


async def open_connection(
    connect: Callable[[], Awaitable[BLEConnection]], busy_timeout: float, max_duration: float | None
) -> BLEConnection:
    """Opens a connection within ``busy_timeout`` seconds and, with ``max_duration``, closes it after that many."""
    try:
        async with asyncio.timeout(busy_timeout):
            connection = await connect()
    except TimeoutError as error:
        raise ConnectionError(f"Couldn't connect within {busy_timeout} s") from error
    if max_duration is not None:
        _spawn(_disconnect_after(connection, max_duration))
    return connection


@contextlib.asynccontextmanager
async def connected(
    connect: Callable[[], Awaitable[BLEConnection]], busy_timeout: float, max_duration: float | None
) -> AsyncGenerator[BLEConnection]:
    """`open_connection` as a block that disconnects when it ends; past ``max_duration`` the block gets
    `TimeoutError`."""
    connection = await open_connection(connect, busy_timeout, None)
    try:
        async with asyncio.timeout(max_duration):
            yield connection
    finally:
        with contextlib.suppress(Exception):  # the device may be gone already
            await asyncio.shield(connection.disconnect())


_tasks: set[asyncio.Task] = set()


def _spawn(coroutine: Awaitable[None]):
    task = asyncio.ensure_future(coroutine)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _disconnect_after(connection: BLEConnection, seconds: float):
    await asyncio.sleep(seconds)
    with contextlib.suppress(Exception):  # disconnected meanwhile
        await connection.disconnect()
