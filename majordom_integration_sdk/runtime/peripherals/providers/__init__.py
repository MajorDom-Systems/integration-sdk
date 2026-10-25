"""Peripherals the runtime runs in its own process and offers as shared endpoints with an ``instance``: the Bluetooth
router (``ble/router``), DNS-SD (``mdns/dns-sd``, the ``discovery`` extra) and SSDP (``upnp/ssdp``)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from majordom_integration_sdk.peripherals import Endpoint, Need, PeripheralChange

log = logging.getLogger(__name__)


class Provider(Protocol):
    """An in-process peripheral. Its needs are bound before any consumer's, and it hears about each change of them."""

    @property
    def name(self) -> str: ...
    @property
    def needs(self) -> Sequence[Need]: ...
    @property
    def endpoint(self) -> Endpoint: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def peripheral_did_change(self, change: PeripheralChange) -> None: ...


def builtin_providers() -> list[Provider]:
    """One of each provider whose extra is installed."""
    from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
    from majordom_integration_sdk.runtime.peripherals.providers.ssdp import SSDPDiscoveryService

    providers: list[Provider] = [BLEService(), SSDPDiscoveryService()]
    try:
        from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
    except ImportError:
        log.warning('No DNS-SD without zeroconf: pip install "majordom-integration-sdk[discovery]"')
    else:
        providers.append(ZeroconfDiscoveryService())
    return providers
