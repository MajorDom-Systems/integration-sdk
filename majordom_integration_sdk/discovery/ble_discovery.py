import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

logger = logging.getLogger(__name__)


@dataclass
class BLEDiscoveryInfo:
    device: BLEDevice
    advertisement: AdvertisementData


class BLEDiscoveryListener(Protocol):
    async def ble_did_discover_device(self, ble: "BLEDiscoveryService", info: BLEDiscoveryInfo): ...
    async def ble_did_update_device(self, ble: "BLEDiscoveryService", info: BLEDiscoveryInfo): ...
    async def ble_did_remove_device(self, ble: "BLEDiscoveryService", info: BLEDiscoveryInfo): ...


def _adv_signature(adv: AdvertisementData) -> tuple:
    """Stable advertisement fingerprint — avoids false updates from object identity changes."""
    return (
        adv.local_name,
        tuple(sorted(adv.service_uuids)),
        tuple(sorted((k, bytes(v)) for k, v in adv.manufacturer_data.items())),
        tuple(sorted((k, bytes(v)) for k, v in adv.service_data.items())),
    )


@dataclass
class _BLETrackedDevice:
    info: BLEDiscoveryInfo
    last_seen_at: float  # monotonic
    signature: tuple = ()


class BLEDiscoveryService:
    """Bleak-based BLE device discovery."""

    def __init__(self):
        self._services: dict[frozenset[UUID], BLEDiscoveryListener] = {}
        self._is_running: bool = False
        self._removal_grace_sec: float = 11.0  # seconds since last seen before did_remove is fired
        self._eviction_interval_sec: float = 10.0
        self._tracked: dict[str, _BLETrackedDevice] = {}  # address -> tracked device
        self._scanner: BleakScanner | None = None
        self._tasks: list[asyncio.Task] = []

    # FUTURE: filter type
    def register(self, listener: BLEDiscoveryListener, service_ids: set[UUID]) -> Callable[[], None]:
        """
        Registers a listener for BLE devices advertising any of the given service UUIDs.
        `ble_did_discover_device` is called on each advertisement received matching the service UUIDs.
        Returns a cancel function — call it on stop to deregister.
        """
        key = frozenset(service_ids)
        self._services[key] = listener

        def cancel():
            self._services.pop(key, None)

        return cancel

    async def start(self):
        self._is_running = True
        self._scanner = BleakScanner(detection_callback=self._on_advertisement)
        # FUTURE: add support for BLE proxies
        await self._scanner.start()
        eviction_task = asyncio.create_task(self._eviction_loop())
        eviction_task.add_done_callback(self._on_task_done)
        self._tasks = [eviction_task]
        logger.debug(f"BLE scanner started, {len(self._services)} service(s) registered")

    async def stop(self):
        self._is_running = False
        if self._scanner:
            await self._scanner.stop()
            self._scanner = None
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.debug(f"BLE scanner stopped, {len(self._services)} service(s) registered")

    def _on_task_done(self, task: asyncio.Task):
        if not task.cancelled() and (exc := task.exception()):
            logger.error(f"BLE task crashed: {exc}")

    def _on_advertisement(self, device: BLEDevice, adv_data: AdvertisementData):
        asyncio.create_task(self._handle_advertisement(device, adv_data))

    async def _handle_advertisement(self, device: BLEDevice, adv_data: AdvertisementData):
        address = device.address
        info = BLEDiscoveryInfo(device, adv_data)
        sig = _adv_signature(adv_data)
        now = monotonic()
        prev = self._tracked.get(address)
        adv_changed = prev is not None and prev.signature != sig

        # update tracking before awaiting listeners — guards against concurrent advertisements
        # for the same address both appearing as new
        self._tracked[address] = _BLETrackedDevice(info=info, last_seen_at=now, signature=sig)

        tasks = []
        for service_ids, listener in self._services.items():
            if not any(str(sid) in adv_data.service_data or str(sid) in adv_data.service_uuids for sid in service_ids):
                continue
            # is_new is per-subscriber match: first time this device matched this filter
            matched_before = prev is not None and any(
                str(sid) in prev.info.advertisement.service_data or str(sid) in prev.info.advertisement.service_uuids
                for sid in service_ids
            )
            if not matched_before:
                tasks.append((True, listener.ble_did_discover_device(self, info)))
            elif adv_changed:
                tasks.append((False, listener.ble_did_update_device(self, info)))

        if tasks:
            is_new = tasks[0][0]
            logger.debug(f"BLE {'discovered' if is_new else 'updated'}: {device.name} ({address})")
            await asyncio.gather(*(coro for _, coro in tasks))

    async def _eviction_loop(self):
        while self._is_running:
            await asyncio.sleep(self._eviction_interval_sec)
            await self._evict_stale()

    async def _evict_stale(self):
        now = monotonic()
        for address, tracked in list(self._tracked.items()):
            if now - tracked.last_seen_at < self._removal_grace_sec:
                continue
            del self._tracked[address]
            tasks = []
            for service_ids, listener in self._services.items():
                adv_data = tracked.info.advertisement
                if any(str(sid) in adv_data.service_data or str(sid) in adv_data.service_uuids for sid in service_ids):
                    tasks.append(listener.ble_did_remove_device(self, tracked.info))
            if tasks:
                logger.debug(f"BLE removed: {tracked.info.device.name} ({address})")
                await asyncio.gather(*tasks)

    async def perform_scan(self):
        """Triggers an immediate eviction check. Scanner runs continuously — no manual scan needed."""
        await self._evict_stale()


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
