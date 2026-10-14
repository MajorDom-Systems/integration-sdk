"""Bluetooth LE for integrations: one service that hears every radio and reaches every device.

Sources feed advertisements in: the hub's own adapters and proxies (e.g. ESPHome ``bluetooth_proxy``).
Listeners register `BLEMatcher`s and hear about matching devices whichever source saw them.
`BLEService.connect` opens a GATT connection through the source that hears the device best.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import UUID

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import OrPattern
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

logger = logging.getLogger(__name__)

OnAdvertisement = Callable[[BLEDevice, AdvertisementData], None]


@dataclass(frozen=True)
class BLEMatcher:
    """What a listener wants to hear about; every field that is set must match."""

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
    async def ble_did_discover_device(self, ble: "BLEService", info: BLEDiscoveryInfo): ...
    async def ble_did_update_device(self, ble: "BLEService", info: BLEDiscoveryInfo): ...
    async def ble_did_remove_device(self, ble: "BLEService", info: BLEDiscoveryInfo): ...


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
    async def write(self, handle: int, data: bytes, response: bool = True) -> None: ...
    async def start_notify(self, handle: int, callback: Callable[[bytes], None]) -> None: ...
    async def stop_notify(self, handle: int) -> None: ...
    async def disconnect(self) -> None: ...


class BLESource(Protocol):
    """A radio that hears advertisements and can connect to devices: a local adapter or a proxy."""

    name: str

    async def start(self, on_advertisement: OnAdvertisement) -> None: ...
    async def stop(self) -> None: ...
    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection: ...


def _adv_signature(adv: AdvertisementData) -> tuple:
    """Stable advertisement fingerprint — avoids false updates from object identity changes."""
    return (
        adv.local_name,
        tuple(sorted(adv.service_uuids)),
        tuple(sorted((k, bytes(v)) for k, v in adv.manufacturer_data.items())),
        tuple(sorted((k, bytes(v)) for k, v in adv.service_data.items())),
    )


def _matches(matchers: list[BLEMatcher], adv: AdvertisementData) -> bool:
    return any(m.matches(adv) for m in matchers)


_EVERY_ADVERTISER = [OrPattern(0, AdvertisementDataType.FLAGS, flags) for flags in (b"\x02", b"\x06", b"\x1a")]


@dataclass
class _BLETrackedDevice:
    info: BLEDiscoveryInfo
    heard: dict[str, tuple[int, float]] = field(default_factory=dict)  # source -> (rssi, monotonic last seen)

    def sources(self, grace: float) -> list[str]:
        """Sources that heard it within ``grace`` seconds, the strongest signal first."""
        now = monotonic()
        recent = [(rssi, source) for source, (rssi, seen) in self.heard.items() if now - seen < grace]
        return [source for _, source in sorted(recent, reverse=True)]


class BLEService:
    """Bluetooth LE across all sources: discovery for listeners, GATT connections on demand."""

    def __init__(self, sources: Iterable[BLESource] | None = None):
        """``sources`` default to every local adapter (each BlueZ ``hciN``) found on start."""
        self._listeners: list[tuple[BLEDiscoveryListener, list[BLEMatcher]]] = []
        self._sources: dict[str, BLESource] = {s.name: s for s in sources or ()}
        self._use_local_adapters = sources is None
        self._is_running: bool = False
        self._pauses = 0
        self._removal_grace_sec: float = 11.0  # seconds unheard by every source before did_remove is fired
        self._eviction_interval_sec: float = 10.0
        self._silence_sec: float = 60.0  # a source unheard this long is restarted
        self._active_until: float = 0.0  # monotonic end of an active scan burst
        self._heard_at: dict[str, float] = {}  # source -> monotonic time it last heard anything
        self._tracked: dict[str, _BLETrackedDevice] = {}  # address -> tracked device
        self._tasks: set[asyncio.Task] = set()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def register(self, listener: BLEDiscoveryListener, matchers: Iterable[BLEMatcher | UUID]) -> Callable[[], None]:
        """
        Registers a listener for BLE devices matching any of ``matchers`` (a UUID matches that service).
        `ble_did_discover_device` is called when a device first matches, `ble_did_update_device` when its
        advertisement changes, `ble_did_remove_device` when no source has heard it for a while.
        Returns a cancel function — call it on stop to deregister.
        """
        entry = (listener, [m if isinstance(m, BLEMatcher) else BLEMatcher(service_uuid=m) for m in matchers])
        self._listeners.append(entry)
        for tracked in self._tracked.values():  # devices heard before it registered are new to it
            if _matches(entry[1], tracked.info.advertisement):
                self._spawn(listener.ble_did_discover_device(self, tracked.info))

        def cancel():
            if entry in self._listeners:
                self._listeners.remove(entry)

        return cancel

    async def add_source(self, source: BLESource):
        """Listen with one more radio, e.g. a proxy that came online."""
        await self.remove_source(source.name)
        self._sources[source.name] = source
        if self._is_running and not self._pauses:
            await self._start_source(source.name)

    async def remove_source(self, name: str):
        if source := self._sources.pop(name, None):
            await source.stop()

    async def start(self):
        if self._use_local_adapters:
            self._sources = {a.name: a for a in _local_adapters()} | self._sources
        self._is_running = True
        await self._start_sources()
        self._spawn(self._eviction_loop())
        logger.debug(f"BLE started with sources {list(self._sources)}, {len(self._listeners)} listener(s)")

    async def stop(self):
        self._is_running = False
        await self._stop_sources()
        for task in list(self._tasks):
            task.cancel()
        logger.debug(f"BLE stopped, {len(self._listeners)} listener(s) registered")

    @contextlib.asynccontextmanager
    async def paused(self, reason: str = "") -> AsyncIterator[None]:
        """Stops listening while something else needs the radios to itself (e.g. a commissioning
        process with its own BlueZ client). Nested pauses resume when the last one ends."""
        logger.debug(f"BLE paused: {reason or 'no reason given'}")
        self._pauses += 1
        if self._pauses == 1:
            await self._stop_sources()
        try:
            yield
        finally:
            self._pauses -= 1
            if not self._pauses and self._is_running:
                await self._start_sources()

    async def connect(self, address: str, on_disconnect: Callable[[], None] = lambda: None) -> BLEConnection:
        """Opens a GATT connection to ``address`` through the source hearing it best, falling back to
        the others. ``on_disconnect`` is called when the connection drops."""
        tracked = self._tracked.get(address)
        candidates = (tracked.sources(self._removal_grace_sec) if tracked else []) or list(self._sources)
        errors = []
        for name in candidates:
            try:
                return await self._sources[name].connect(address, on_disconnect)
            except Exception as e:
                errors.append(f"{name}: {e}")
        raise ConnectionError(f"Can't connect to {address}: {'; '.join(errors) or 'no Bluetooth sources'}")

    async def scan_actively(self, duration: float):
        """Scans actively for ``duration`` seconds (a pairing window): names often come only in the scan
        responses a passive scan never asks for."""
        self._active_until = max(self._active_until, monotonic() + duration)
        await self._rescan()
        self._spawn(self._rescan(after=duration))

    def devices(self) -> list[BLEDiscoveryInfo]:
        """Every device heard recently, as the source hearing it best sees it."""
        return [tracked.info for tracked in self._tracked.values()]

    def best_device(self, address: str) -> BLEDevice | None:
        """bleak's device as the source hearing it best has it, for libraries that connect through bleak."""
        tracked = self._tracked.get(address)
        return tracked.info.device if tracked else None

    # Internals

    async def _start_sources(self):
        await asyncio.gather(*(self._start_source(name) for name in self._sources))

    def _scans_passively(self) -> bool:
        return monotonic() >= self._active_until

    async def _rescan(self, after: float = 0):
        """Restarts the local adapters whose scan no longer fits (a burst began or ended)."""
        await asyncio.sleep(after)
        if not self._is_running or self._pauses:
            return
        passive = self._scans_passively()
        for name, source in list(self._sources.items()):
            if isinstance(source, LocalAdapter) and source.passive != passive:
                await source.stop()
                await self._start_source(name)

    async def _start_source(self, name: str):
        self._heard_at[name] = monotonic()  # one that fails to start is retried once it counts as silent
        source = self._sources[name]
        if isinstance(source, LocalAdapter):
            source.passive = self._scans_passively()
        try:
            await source.start(self._advertisement_handler(name))
        except Exception as e:
            logger.warning(f"BLE source {name} failed to start: {e}")

    async def _stop_sources(self):
        await asyncio.gather(*(s.stop() for s in self._sources.values()), return_exceptions=True)

    def _advertisement_handler(self, source: str) -> OnAdvertisement:
        def on_advertisement(device: BLEDevice, adv_data: AdvertisementData):
            self._heard_at[source] = monotonic()
            self._spawn(self._handle_advertisement(source, device, adv_data))

        return on_advertisement

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            logger.error(f"BLE task crashed: {exc}")

    async def _handle_advertisement(self, source: str, device: BLEDevice, adv_data: AdvertisementData):
        address = device.address
        prev = self._tracked.get(address)
        before = prev.info.advertisement if prev else None

        # update tracking before awaiting listeners — guards against concurrent advertisements
        # for the same address both appearing as new
        tracked = prev or _BLETrackedDevice(BLEDiscoveryInfo(device, adv_data, source, adv_data.rssi, monotonic()))
        tracked.heard[source] = (adv_data.rssi, monotonic())
        self._tracked[address] = tracked
        # a weaker source only keeps the device alive: sources hear different subsets of the
        # advertisement (e.g. no scan response), so mixing them would report false updates
        if tracked.sources(self._removal_grace_sec)[0] != source:
            return
        tracked.info = info = BLEDiscoveryInfo(device, adv_data, source, adv_data.rssi, monotonic())
        adv_changed = before is not None and _adv_signature(before) != _adv_signature(adv_data)

        tasks = []
        for listener, matchers in list(self._listeners):
            if not _matches(matchers, adv_data):
                continue
            # is_new is per-subscriber match: first time this device matched this filter
            if before is None or not _matches(matchers, before):
                tasks.append((True, listener.ble_did_discover_device(self, info)))
            elif adv_changed:
                tasks.append((False, listener.ble_did_update_device(self, info)))

        if tasks:
            is_new = tasks[0][0]
            logger.debug(f"BLE {'discovered' if is_new else 'updated'}: {device.name} ({address}) via {source}")
            await asyncio.gather(*(coro for _, coro in tasks))

    async def _eviction_loop(self):
        while self._is_running:
            await asyncio.sleep(self._eviction_interval_sec)
            await self._evict_stale()
            await self._revive_silent()

    async def _revive_silent(self):
        """Restarts sources that heard nothing for a while: their radio went away under them without a word
        (bluetoothd restarted, the adapter reset) and they won't come back by themselves."""
        if self._pauses:
            return
        for name, source in list(self._sources.items()):
            if monotonic() - self._heard_at.get(name, 0) > self._silence_sec:
                logger.info(f"BLE source {name} heard nothing for {self._silence_sec:.0f} s, restarting it")
                with contextlib.suppress(Exception):
                    await source.stop()
                await self._start_source(name)

    async def _evict_stale(self):
        for address, tracked in list(self._tracked.items()):
            if tracked.sources(self._removal_grace_sec):
                continue
            del self._tracked[address]
            adv_data = tracked.info.advertisement
            tasks = [
                listener.ble_did_remove_device(self, tracked.info)
                for listener, matchers in list(self._listeners)
                if _matches(matchers, adv_data)
            ]
            if tasks:
                logger.debug(f"BLE removed: {tracked.info.device.name} ({address})")
                await asyncio.gather(*tasks)

    async def perform_scan(self):
        """Triggers an immediate eviction check. Sources listen continuously — no manual scan needed."""
        await self._evict_stale()


#: The previous name, kept while integrations move to `BLEService`.
BLEDiscoveryService = BLEService


class LocalAdapter:
    """A Bluetooth adapter of this machine, through bleak (BlueZ on Linux)."""

    def __init__(self, adapter: str | None = None):
        self.name = adapter or "local"
        self._options = {"bluez": {"adapter": adapter}} if adapter else {}
        self._scanner: BleakScanner | None = None
        self._devices: dict[str, BLEDevice] = {}  # address -> last seen; connecting by it skips a scan
        self.passive = False  # set: a passive scan of every advertiser, the listeners' matchers choose
        self._passive_refused = False

    async def start(self, on_advertisement: OnAdvertisement):
        def on_detection(device: BLEDevice, adv_data: AdvertisementData):
            self._devices.pop(device.address, None)  # re-added last: the dict stays oldest first
            self._devices[device.address] = device
            if len(self._devices) > 1024:  # phones rotate their address: drop the oldest
                del self._devices[next(iter(self._devices))]
            on_advertisement(device, adv_data)

        # BlueZ refuses a passive scan without --experimental (Raspberry Pi OS's default): then it scans actively. With
        # it, a Pi 5 (BlueZ 5.82, no offload) passes adverts only through patterns on the flags (2026-10-14), as in HA.
        if self.passive and not self._passive_refused:
            bluez = {**self._options.get("bluez", {}), "or_patterns": _EVERY_ADVERTISER}
            self._scanner = BleakScanner(detection_callback=on_detection, scanning_mode="passive", bluez=bluez)
            try:
                return await self._scanner.start()
            except Exception as e:  # BlueZ's advertisement monitor is missing (an old or non-experimental bluetoothd)
                logger.warning(f"BLE {self.name} can't scan passively, scanning actively: {e}")
                self._passive_refused = True
        self.passive = False
        self._scanner = BleakScanner(detection_callback=on_detection, **self._options)
        await self._scanner.start()

    async def stop(self):
        scanner, self._scanner = self._scanner, None
        if scanner:
            await scanner.stop()

    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection:
        device = self._devices.get(address, address)
        client = BleakClient(device, disconnected_callback=lambda _: on_disconnect(), **self._options)
        scanner = self._scanner
        if scanner:  # many adapters (the Raspberry Pi's included) stall a connect while discovering
            await scanner.stop()
        try:
            await client.connect()
        finally:
            if scanner and scanner is self._scanner:
                await scanner.start()
        return _BleakConnection(client)


class _BleakConnection:
    def __init__(self, client: BleakClient):
        self._client = client

    @property
    def mtu(self) -> int:
        return self._client.mtu_size

    async def services(self) -> list[GattService]:
        return [
            GattService(
                service.uuid,
                service.handle,
                tuple(GattCharacteristic(c.uuid, c.handle, tuple(c.properties)) for c in service.characteristics),
            )
            for service in self._client.services
        ]

    async def read(self, handle: int) -> bytes:
        return bytes(await self._client.read_gatt_char(handle))

    async def write(self, handle: int, data: bytes, response: bool = True):
        await self._client.write_gatt_char(handle, data, response=response)

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        await self._client.start_notify(handle, lambda _, data: callback(bytes(data)))

    async def stop_notify(self, handle: int):
        await self._client.stop_notify(handle)

    async def disconnect(self):
        await self._client.disconnect()


def _local_adapters() -> list[LocalAdapter]:
    """One source per BlueZ adapter (hci0, a USB dongle as hci1, …); the default adapter elsewhere."""
    names = sorted(path.name for path in Path("/sys/class/bluetooth").glob("hci*") if ":" not in path.name)
    return [LocalAdapter(name) for name in names] or [LocalAdapter()]


# Example usage

if __name__ == "__main__":
    # logging.basicConfig()
    # logger.setLevel(logging.DEBUG)

    async def main():
        discovery = BLEDiscoveryService()

        class SwitchBotListener:
            async def ble_did_discover_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
                print(f"Found: {info.device.name} ({info.device.address})")

            async def ble_did_update_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
                print(f"Updated: {info.device.name} ({info.device.address})")

            async def ble_did_remove_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
                print(f"Removed: {info.device.name} ({info.device.address})")

        switchbot_services = {
            UUID("00000d00-0000-1000-8000-00805f9b34fb"),
            UUID("0000fd3d-0000-1000-8000-00805f9b34fb"),
            UUID("cba20d00-224d-11e6-9fb8-0002a5d5c51b"),
        }

        discovery.register(SwitchBotListener(), switchbot_services)
        await discovery.start()

        while True:
            await asyncio.sleep(1)

    asyncio.run(main())
