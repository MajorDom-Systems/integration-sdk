"""What a runtime runs by default, given explicitly: the defaults keep a composition root short and replaceable. Each
call builds new ones, so no two runtimes share a peripheral.

Under the Hub, hardware first (the order given is the order of preference)::

    Runtime(data, [Pinned(*mine), FixedPeripherals(*soldered), *default_peripherals(), *integrations], context=...)

In library mode, one integration with what it needs::

    Runtime(data, [*default_peripherals(), HueController(platform)])
    Runtime(data, [HostNetwork(), ZeroconfDiscoveryService(), HueController(platform)])
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from majordom_integration_sdk.peripherals import Peripheral
from majordom_integration_sdk.runtime.peripherals.hardware.host import BluetoothAdapters, HostNetwork
from majordom_integration_sdk.runtime.peripherals.hardware.usb import UsbScanner
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.ssdp import SSDPDiscoveryService
from majordom_integration_sdk.runtime.peripherals.services.builtin import otbr, silabs_multiprotocol
from majordom_integration_sdk.runtime.peripherals.services.compose import Compose

log = logging.getLogger(__name__)


def default_peripherals() -> list[Peripheral[Any]]:
    """Everything the SDK runs around integrations, in the order it binds: the sticks plugged in (by
    `default_usb_drivers`) before the containers that serve over them, Docker Compose that runs those, the
    multiprotocol border router before the standalone one, the Bluetooth router before the integrations, so it takes
    every adapter. On-demand ones run only while used."""
    zeroconf = _zeroconf()
    return [
        UsbScanner(),
        BluetoothAdapters(),
        HostNetwork(),
        Compose(),
        silabs_multiprotocol(),
        otbr(),
        BLEService(),
        *([zeroconf()] if zeroconf else []),
        SSDPDiscoveryService(),
    ]


@functools.cache  # said once
def _zeroconf() -> type[Peripheral[Any]] | None:
    try:
        from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
    except ImportError:
        log.warning('No DNS-SD without zeroconf: pip install "majordom-integration-sdk[discovery]"')
        return None
    return ZeroconfDiscoveryService


__all__ = ["default_peripherals"]
