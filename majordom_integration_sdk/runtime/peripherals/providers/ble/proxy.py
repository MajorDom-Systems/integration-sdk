"""Bluetooth sources integrations lend the router."""

from __future__ import annotations

from collections.abc import Sequence

from majordom_integration_sdk.capabilities.ble import BLESource
from majordom_integration_sdk.peripherals import Endpoint, Peripheral


class BLEProxy(Peripheral):
    """A Bluetooth source an integration brings along while it has it, e.g. an ESPHome node's proxy as
    ``ble/esphome-proxy``: the router binds it and hears through ``source``. Add one to your ``self.peripherals`` when
    the source connects, remove it when it goes, and tell your ``output`` each time; clear them when you stop.
    """

    def __init__(self, source: BLESource, dialect: str = "esphome-proxy"):
        self.name, self.source, self.dialect = source.name, source, dialect

    @property
    def provides(self) -> Sequence[Endpoint]:
        return [Endpoint("ble", self.dialect, self.source.name, instance=self.source)]
