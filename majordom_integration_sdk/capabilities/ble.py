"""The ``ble/router`` contract: one Bluetooth LE router that hears every radio and reaches every device.

Integrations need it as ``Need("ble", ["router"])`` and get it at ``self.peripherals.instance("ble")``; never raw
Bluetooth, which the router holds. Its sources are the radios the runtime binds to it (``ble/bluez`` adapters,
``ble/silabs-cpc-hci``, ``ble/esphome-proxy`` nodes an integration offers): `BLESource`s.

Listeners register `BLEMatcher`s and hear about matching devices whichever source saw them.
`BLERouter.connect` opens a GATT connection through the source that hears the device best;
`BLERouter.exclusive` leases that one source to a device for as long as pairing it takes.
Credentials go to a device only over a link a `SecureChannel` (`ble_security`) has secured first.

bleak's advertisement types describe what is heard; this module imports them for type checking only.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

type OnAdvertisement = Callable[[BLEDevice, AdvertisementData], None]


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
    async def write(self, handle: int, data: bytes, is_response_needed: bool = True) -> None: ...
    async def start_notify(self, handle: int, callback: Callable[[bytes], None]) -> None: ...
    async def stop_notify(self, handle: int) -> None: ...
    async def disconnect(self) -> None: ...


@runtime_checkable
class PairableBLEConnection(BLEConnection, Protocol):
    """A connection that can encrypt its link by pairing (LE Secure Connections where both ends support it)."""

    async def pair(self) -> None:
        """Pairs "Just Works", encrypting the link. Raises `BLEPairingError`."""
        ...

    async def unpair(self) -> None:
        """Forgets the bond; the link may drop."""
        ...


class BLESource(Protocol):
    """A radio that hears advertisements and can connect to devices: a local adapter or a proxy. Optional
    attributes the router reads: a true ``is_pairable`` when its connections are `PairableBLEConnection`s, and
    ``free_slots``, the connections it can still open (a proxy has a few), None when it can't tell."""

    name: str

    async def start(self, on_advertisement: OnAdvertisement) -> None: ...
    async def stop(self) -> None: ...
    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection: ...


@runtime_checkable
class PausableBLESource(BLESource, Protocol):
    """A source that can stop hearing without letting go of its radio, e.g. a proxy whose connections only
    work while it stays subscribed; a lease pauses one rather than stopping it."""

    async def pause_scanning(self) -> None: ...
    async def resume_scanning(self) -> None: ...


@runtime_checkable
class ScanModeBLESource(BLESource, Protocol):
    """A source that scans passively (every advertiser, the listeners' matchers choose) or actively (asking for the
    scan responses that often carry the name): the router switches it for a pairing window. Called before `start`
    too; a running source applies a change itself."""

    async def set_scan_mode(self, is_active: bool) -> None: ...


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

    async def connect(self, on_disconnect: Callable[[], None] = lambda: None) -> BLEConnection:
        return await self._source.connect(self.address, on_disconnect)


class BLERouter(Protocol):
    """Bluetooth LE across all sources: discovery for listeners, GATT connections on demand."""

    def register(self, listener: BLEDiscoveryListener, matchers: Iterable[BLEMatcher | UUID]) -> Callable[[], None]:
        """Registers a listener for BLE devices matching any of ``matchers`` (a UUID matches that service).
        `ble_did_discover_device` is called when a device first matches, `ble_did_update_device` when its
        advertisement changes, `ble_did_remove_device` when no source has heard it for a while; devices heard
        before it registered are new to it. Returns a cancel function — call it on stop to deregister."""
        ...

    async def connect(self, address: str, on_disconnect: Callable[[], None] = lambda: None) -> BLEConnection:
        """Opens a GATT connection to ``address`` through the source hearing it best, falling back to the others;
        leased sources are skipped. ``on_disconnect`` is called when the connection drops."""
        ...

    def exclusive(
        self, address: str, *, owner: str, timeout: float = 30, wait: float = 1, is_pairing_needed: bool = False
    ) -> contextlib.AbstractAsyncContextManager[BLELease]:
        """Leases the source hearing ``address`` best to ``owner`` (e.g. while pairing it): that source pauses
        scanning and others' connects avoid it; the rest keep going. A leased source is waited for ``wait`` seconds
        before the next best is tried, round after round, until ``timeout`` raises `BLEBusyError`. With
        ``is_pairing_needed``, only sources that can pair are leased (`BLEPairingError` when none hears it)."""
        ...

    async def scan_actively(self, duration: float) -> None:
        """Scans actively for ``duration`` seconds (a pairing window): names often come only in the scan
        responses a passive scan never asks for."""
        ...

    def devices(self) -> list[BLEDiscoveryInfo]:
        """Every device heard recently, as the source hearing it best sees it."""
        ...
