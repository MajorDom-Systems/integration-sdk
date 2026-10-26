"""Bluetooth sources integrations lend the runtime's router."""

from __future__ import annotations

from collections.abc import Sequence

from majordom_integration_sdk.capabilities.ble import BLESource
from majordom_integration_sdk.peripherals.endpoint import Endpoint
from majordom_integration_sdk.peripherals.peripheral import Peripheral


class BLEProxy(Peripheral):
    """A Bluetooth source an integration brings along while it has it, e.g. an ESPHome node's proxy as
    ``ble/esphome-proxy``: the router binds it and hears through ``source``. Add one to what you provide when the source
    connects, remove it when it goes, and tell your ``runtime`` each time; they go when you stop. ``name`` is what it is
    known by after you, ``esphome/<name>``: the source's name by default, else e.g. the node's Bluetooth MAC.
    """

    def __init__(self, source: BLESource, name: str | None = None, dialect: str = "esphome-proxy"):
        self.name, self.source, self.dialect = name or source.name, source, dialect

    @property
    def provides(self) -> Sequence[Endpoint]:
        return [Endpoint("ble", self.dialect, self.source.name, instance=self.source)]
