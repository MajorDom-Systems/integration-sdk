"""What a runtime runs by default, given explicitly: the defaults keep a composition root short and replaceable.

Under the Hub::

    Runtime(data, peripherals=[*DEFAULT_PERIPHERALS, FixedPeripherals(*soldered), *integrations], context=...)

In library mode, one integration with what it needs::

    Runtime(data, peripherals=[*DEFAULT_PERIPHERALS, HueController(platform)])
    Runtime(data, peripherals=[HostNetwork(), ZeroconfDiscoveryService(), HueController(platform)])

The instances are created once: give them to one runtime at a time.
"""

from __future__ import annotations

import logging

from majordom_integration_sdk.peripherals import Peripheral
from majordom_integration_sdk.runtime.peripherals.hardware.drivers import DEFAULT_USB_DRIVERS
from majordom_integration_sdk.runtime.peripherals.hardware.host import BluetoothAdapters, HostNetwork
from majordom_integration_sdk.runtime.peripherals.hardware.usb import UsbScanner
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.ssdp import SSDPDiscoveryService
from majordom_integration_sdk.runtime.peripherals.services.builtin import otbr, silabs_multiprotocol

log = logging.getLogger(__name__)


def _zeroconf() -> list[Peripheral]:
    try:
        from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
    except ImportError:
        log.warning('No DNS-SD without zeroconf: pip install "majordom-integration-sdk[discovery]"')
        return []
    return [ZeroconfDiscoveryService()]


#: Everything the SDK runs around integrations, in the order it binds: the sticks plugged in (by `DEFAULT_USB_DRIVERS`)
#: before the containers that serve over them, the multiprotocol border router before the standalone one, the
#: Bluetooth router before the integrations, so it takes every adapter. On-demand ones run only while used.
DEFAULT_PERIPHERALS: tuple[Peripheral, ...] = (
    UsbScanner(),
    BluetoothAdapters(),
    HostNetwork(),
    silabs_multiprotocol,
    otbr,
    BLEService(),
    *_zeroconf(),
    SSDPDiscoveryService(),
)

__all__ = ["DEFAULT_PERIPHERALS", "DEFAULT_USB_DRIVERS"]
