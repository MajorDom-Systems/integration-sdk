"""Drivers that recognise USB devices for the runtime's USB scanner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from majordom_integration_sdk.peripherals.endpoint import Endpoint
from majordom_integration_sdk.peripherals.peripheral import Peripheral

USB_DRIVER = "usb-driver"  # the family a driver provides itself as, to the scanners that need every driver


class UsbDriver(Peripheral):
    """One kind of USB device: recognises which firmware a device runs and says what each firmware serves.

    The scanner asks its own drivers first, then the ones integrations provide next to their controller: Zigbee's
    tells the radios only zigpy knows apart. ``firmwares`` maps a firmware to what it serves: ``{device}`` in a URL is
    the device's path, ``params`` its serial settings (soldered hardware may wire them differently).
    """

    is_on_demand = True  # only while a scanner uses it
    firmwares: ClassVar[Mapping[str, Sequence[Endpoint]]] = {}
    bluetooth: ClassVar[frozenset[str]] = frozenset()  # firmwares that serve Bluetooth too (multiprotocol over CPC)

    @property
    def provides(self) -> Sequence[Endpoint]:
        return [Endpoint(USB_DRIVER, self.name, self.name, is_shared=True, instance=self)]

    async def identify(self, path: str) -> str | None:
        """The firmware the device at ``path`` runs, or None when it isn't one of its kind."""
        return None

    def serves(self, firmware: str | None, path: str, params: Mapping[str, Any] | None = None) -> list[Endpoint]:
        """What ``firmware`` serves on the device at ``path``, ``params`` overriding its serial settings."""
        endpoints = self.firmwares.get(firmware or "", [])
        return [replace(e, url=e.url.format(device=path), params={**e.params, **(params or {})}) for e in endpoints]

    def flashable(self, path: str, channel: str) -> list[str]:
        """Firmwares the device at ``path`` can be switched to on the ``stable`` or ``dev`` channel."""
        return []

    async def flash(self, path: str, firmware: str, channel: str, cache: Path):
        raise NotImplementedError(f"{type(self).__name__} can't flash")
