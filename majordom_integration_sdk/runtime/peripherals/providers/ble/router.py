"""The runtime's ``ble/router``: one router over every Bluetooth source the runtime binds to it (see
`majordom_integration_sdk.capabilities.ble` for the contract integrations use).

It needs every raw radio, ``ble/bluez`` adapters, ``ble/silabs-cpc-hci`` and the ``ble/esphome-proxy`` nodes ESPHome
brings along (`BLEProxy`), and provides itself as the one shared ``ble/router``, on demand. Sources come and go as the
runtime rebinds it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable, Iterable
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

from majordom_integration_sdk.capabilities.ble import (
    BLEBusyError,
    BLEConnection,
    BLEDiscoveryInfo,
    BLEDiscoveryListener,
    BLELease,
    BLEMatcher,
    BLEPairingError,
    BLESource,
    OnAdvertisement,
    PausableBLESource,
    ScanModeBLESource,
)
from majordom_integration_sdk.peripherals import Endpoint, Need, Peripheral, PeripheralChange

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

logger = logging.getLogger(__name__)

RAW_BLE = ["bluez", "silabs-cpc-hci", "esphome-proxy"]  # every radio the router hears through


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


@dataclass
class _BLETrackedDevice:
    info: BLEDiscoveryInfo
    heard: dict[str, tuple[int, float]] = field(default_factory=dict)  # source -> (rssi, monotonic last seen)
    name: str | None = None  # the last local name heard: a passive scan misses the scan responses that carry it

    def sources(self, grace: float) -> list[str]:
        """Sources that heard it within ``grace`` seconds, the strongest signal first."""
        now = monotonic()
        recent = [(rssi, source) for source, (rssi, seen) in self.heard.items() if now - seen < grace]
        return [source for _, source in sorted(recent, reverse=True)]


class BLEService(Peripheral):
    """Bluetooth LE across all sources: discovery for listeners, GATT connections on demand. A `BLERouter`."""

    name = "ble-router"
    needs = [Need("ble", RAW_BLE, is_many=True, is_required=False)]
    is_on_demand = True

    def __init__(self, sources: Iterable[BLESource] = ()):
        """``sources``: radios to hear through besides the ones the runtime binds."""
        self._listeners: list[tuple[BLEDiscoveryListener, list[BLEMatcher]]] = []
        self._sources: dict[str, BLESource] = {s.name: s for s in sources}
        self._bound: dict[tuple[str, int], str] = {}  # bound endpoint (url, instance) -> the source it became
        self._is_running: bool = False
        self._leases: dict[str, asyncio.Lock] = {}  # source -> held while it is leased
        self._leased: dict[str, str] = {}  # source -> the address it is leased for: kept while its radio is silenced
        self._removal_grace_sec: float = 11.0  # seconds unheard by every source before did_remove is fired
        self._eviction_interval_sec: float = 10.0
        self._silence_sec: float = 60.0  # a source unheard this long is restarted
        self._active_until: float = 0.0  # monotonic end of an active scan burst
        self._name_burst_sec: float = 5.0  # an active burst to hear the name of a new device advertising none
        self._name_burst_interval_sec: float = 60.0  # at most one such burst this often
        self._name_burst_at: float = float("-inf")  # monotonic start of the last one
        self._heard_at: dict[str, float] = {}  # source -> monotonic time it last heard anything
        self._tracked: dict[str, _BLETrackedDevice] = {}  # address -> tracked device
        self._tasks: set[asyncio.Task] = set()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def endpoint(self) -> Endpoint:
        """This router, shared by every consumer: pass it to a controller in library mode."""
        return Endpoint("ble", "router", "router", is_shared=True, instance=self)

    @property
    def provides(self) -> list[Endpoint]:
        return [self.endpoint]

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """The runtime bound other radios to it: hear through the new ones, let go of the lost ones."""
        await self._follow_bound()
        return True

    async def _follow_bound(self):
        bound = {(e.url, id(e.instance)): e for e in self.peripherals.endpoints("ble")}
        for key in self._bound.keys() - bound.keys():
            await self.remove_source(self._bound.pop(key))
        for key in bound.keys() - self._bound.keys():
            try:
                source = source_of(bound[key])
            except Exception as e:  # a radio that can't be used here (a missing extra) leaves the others working
                logger.warning(f"BLE can't use {bound[key].dialect} {bound[key].url}: {e}")
                continue
            self._bound[key] = source.name
            await self.add_source(source)

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
        if self._is_running and self._may_start(source.name):
            await self._start_source(source.name)

    async def remove_source(self, name: str):
        """Stop listening with a radio; one that fails to stop (its node went away) is let go of all the same."""
        if source := self._sources.pop(name, None):
            try:
                await source.stop()
            except Exception as e:
                logger.warning(f"BLE source {name} failed to stop: {e}")

    async def start(self):
        await self._follow_bound()
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
    async def exclusive(
        self, address: str, *, owner: str, timeout: float = 30, wait: float = 1, is_pairing_needed: bool = False
    ) -> AsyncGenerator[BLELease]:
        """Leases the source hearing ``address`` best to ``owner`` (e.g. while pairing it): that source stops
        scanning and others' connects avoid it; the rest keep going. A leased source is waited for ``wait``
        seconds before the next best is tried, round after round, until ``timeout`` raises `BLEBusyError`.
        With ``is_pairing_needed``, only sources that can pair are leased (`BLEPairingError` when none hears it).
        The device stays tracked until the lease ends, though no radio may hear it meanwhile."""
        name = await self._acquire(address, owner, timeout, wait, is_pairing_needed)
        self._leased[name] = address
        try:
            source = self._sources[name]
            with contextlib.suppress(Exception):
                # paused, not stopped: a proxy answers connects only while it stays subscribed
                await (source.pause_scanning() if isinstance(source, PausableBLESource) else source.stop())
            yield BLELease(address, name, owner, self._sources[name])
        finally:
            await asyncio.shield(self._release(name, owner))

    async def connect(self, address: str, on_disconnect: Callable[[], None] = lambda: None) -> BLEConnection:
        """Opens a GATT connection to ``address`` through the source hearing it best, falling back to
        the others; leased sources are skipped. ``on_disconnect`` is called when the connection drops."""
        candidates = self._candidates(address)
        free = [name for name in candidates if not self._is_leased(name)]
        if candidates and not free:
            raise BLEBusyError(f"all Bluetooth sources that hear {address} are busy")
        errors = []
        for name in free:
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

    # Internals

    def _candidates(self, address: str) -> list[str]:
        """Sources to reach ``address`` through, the strongest first; a proxy with no connection slot left last."""
        tracked = self._tracked.get(address)
        names = (tracked.sources(self._removal_grace_sec) if tracked else []) or list(self._sources)
        return sorted(names, key=lambda name: getattr(self._sources.get(name), "free_slots", None) == 0)

    def _is_leased(self, name: str) -> bool:
        return name in self._leases and self._leases[name].locked()

    def _may_start(self, name: str) -> bool:
        """A leased source starts only when it can pause (`_start_source` pauses it): the lease connects through it."""
        return not self._is_leased(name) or isinstance(self._sources[name], PausableBLESource)

    async def _acquire(
        self, address: str, owner: str, timeout: float, wait: float, is_pairing_needed: bool = False
    ) -> str:
        """Locks the first candidate source that frees up within ``wait``, cycling until ``timeout``."""
        deadline = monotonic() + timeout
        while True:
            if not (candidates := self._candidates(address)):
                raise ConnectionError(f"Can't reach {address}: no Bluetooth sources")
            if is_pairing_needed and not (
                candidates := [n for n in candidates if getattr(self._sources[n], "is_pairable", False)]
            ):
                raise BLEPairingError(f"No Bluetooth source that hears {address} can pair")
            for name in candidates:
                lock = self._leases.setdefault(name, asyncio.Lock())
                if (remaining := deadline - monotonic()) <= 0:
                    raise BLEBusyError(f"all Bluetooth sources that hear {address} are busy")
                try:
                    async with asyncio.timeout(min(wait, remaining)):
                        await lock.acquire()
                except TimeoutError:
                    continue
                if name in self._sources:  # not removed meanwhile
                    logger.info(f"BLE source {name} leased to {owner} for {address}")
                    return name
                lock.release()

    async def _release(self, name: str, owner: str):
        try:
            if self._is_running and name in self._sources:
                await self._resume_source(name)
        finally:
            self._leased.pop(name, None)
            self._leases[name].release()
            logger.info(f"BLE source {name} released by {owner}")

    async def _resume_source(self, name: str):
        source = self._sources[name]
        if not isinstance(source, PausableBLESource):
            return await self._start_source(name)
        self._heard_at[name] = monotonic()
        try:
            await source.resume_scanning()
        except Exception as e:
            logger.warning(f"BLE source {name} failed to resume: {e}")

    async def _start_sources(self):
        # a lease can outlive a stop: its source starts too when it can pause, so the lease connects through it
        names = [name for name in self._sources if self._may_start(name)]
        await asyncio.gather(*(self._start_source(name) for name in names))

    def _scans_passively(self) -> bool:
        return monotonic() >= self._active_until

    async def _rescan(self, after: float = 0):
        """Switches every source that can between passive and active scanning (a burst began or ended)."""
        await asyncio.sleep(after)
        if not self._is_running:
            return
        active = not self._scans_passively()
        for name, source in list(self._sources.items()):
            if isinstance(source, ScanModeBLESource) and not self._is_leased(name):
                try:
                    await source.set_scan_mode(active)
                except Exception as e:
                    logger.warning(f"BLE source {name} failed to switch its scan: {e}")

    async def _start_source(self, name: str):
        self._heard_at[name] = monotonic()  # one that fails to start is retried once it counts as silent
        source = self._sources[name]
        try:
            if isinstance(source, ScanModeBLESource):
                await source.set_scan_mode(not self._scans_passively())
            await source.start(self._advertisement_handler(name))
            if isinstance(source, PausableBLESource):  # paused exactly while leased, though a lease ended while stopped
                await (source.pause_scanning() if self._is_leased(name) else source.resume_scanning())
        except Exception as e:
            logger.warning(f"BLE source {name} failed to start: {e}")

    async def _stop_sources(self):
        """Stop every radio, and let go of the ones it was bound to: the runtime binds them anew before it starts."""
        await asyncio.gather(*(s.stop() for s in self._sources.values()), return_exceptions=True)
        for name in self._bound.values():
            self._sources.pop(name, None)
        self._bound.clear()

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
        if adv_data.local_name:
            tracked.name = adv_data.local_name
        elif tracked.name:
            adv_data = adv_data._replace(local_name=tracked.name)
        elif prev is None:
            self._burst_for_name()
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

    def _burst_for_name(self):
        """Scans actively for a moment, bounded to one burst per interval, so a new device's scan response is heard."""
        if not self._scans_passively() or monotonic() - self._name_burst_at < self._name_burst_interval_sec:
            return
        self._name_burst_at = monotonic()
        self._spawn(self.scan_actively(self._name_burst_sec))

    async def _eviction_loop(self):
        while self._is_running:
            await asyncio.sleep(self._eviction_interval_sec)
            await self._evict_stale()
            await self._revive_silent()

    async def _revive_silent(self):
        """Restarts sources that heard nothing for a while: their radio went away under them without a word
        (bluetoothd restarted, the adapter reset) and they won't come back by themselves. One whose loss is reported
        otherwise (``is_loss_reported``) may just hear no one, and a restart would drop the links it proxies."""
        for name, source in list(self._sources.items()):
            if getattr(source, "is_loss_reported", False) or self._is_leased(name):
                continue
            if monotonic() - self._heard_at.get(name, 0) > self._silence_sec:
                logger.info(f"BLE source {name} heard nothing for {self._silence_sec:.0f} s, restarting it")
                with contextlib.suppress(Exception):
                    await source.stop()
                await self._start_source(name)

    async def _evict_stale(self):
        for address, tracked in list(self._tracked.items()):
            if tracked.sources(self._removal_grace_sec) or address in self._leased.values():
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


def source_of(endpoint: Endpoint) -> BLESource:
    """The source a bound raw-BLE endpoint is: the one it carries (an ESPHome proxy), else a BlueZ adapter."""
    if endpoint.instance is not None:
        return endpoint.instance
    if endpoint.dialect == "silabs-cpc-hci":
        from majordom_integration_sdk.runtime.peripherals.providers.ble.cpc_hci import source_of as cpc_source

        return cpc_source(endpoint)
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter  # the ble extra

    return LocalAdapter(endpoint.url or None)


# Example usage

if __name__ == "__main__":
    # logging.basicConfig()
    # logger.setLevel(logging.DEBUG)

    async def main():
        from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

        discovery = BLEService([LocalAdapter()])

        class SwitchBotListener:
            async def ble_did_discover_device(self, ble: BLEService, info: BLEDiscoveryInfo):
                print(f"Found: {info.device.name} ({info.device.address})")

            async def ble_did_update_device(self, ble: BLEService, info: BLEDiscoveryInfo):
                print(f"Updated: {info.device.name} ({info.device.address})")

            async def ble_did_remove_device(self, ble: BLEService, info: BLEDiscoveryInfo):
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
