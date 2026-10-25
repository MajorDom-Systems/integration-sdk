"""What the host itself serves: its network and its Bluetooth adapters."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from glob import glob
from pathlib import Path

from majordom_integration_sdk.peripherals import Endpoint, Peripheral

log = logging.getLogger(__name__)

POLL = 2  # seconds between looks at the adapters there are


class HostNetwork(Peripheral):
    """The host's IP network, shared by everything that talks over it: ``ip``."""

    name = "host"
    provides = [Endpoint("ip", is_shared=True)]


class BluetoothAdapters(Peripheral):
    """Every Bluetooth adapter of the host as its own single-use ``ble/bluez`` endpoint, with its ``index`` (the router
    takes them all, unless one is reserved); off Linux, bleak's default adapter on the OS's own stack. Adapters that
    appear or disappear are followed."""

    name = "bluetooth-adapters"

    def __init__(self):
        self._adapters: list[str] = []
        self._watcher: asyncio.Task | None = None

    @property
    def provides(self) -> Sequence[Endpoint]:
        return [Endpoint("ble", "bluez", name, params={"index": name.removeprefix("hci")}) for name in self._adapters]

    async def start(self):
        self._adapters = _adapters()
        self._watcher = asyncio.create_task(self._watch(), name="bluetooth-adapters.watch")

    async def stop(self):
        if self._watcher:
            self._watcher.cancel()
        self._adapters = []

    async def _watch(self):
        while True:
            await asyncio.sleep(POLL)
            if (found := _adapters()) != self._adapters:
                self._adapters = found
                try:
                    await self.runtime.peripheral_did_change_provided(self)
                except Exception:
                    log.exception("Following the Bluetooth adapters failed")


def _adapters() -> list[str]:
    if sys.platform != "linux":
        return [""]
    return sorted(Path(path).name for path in glob("/sys/class/bluetooth/hci*") if ":" not in Path(path).name)
