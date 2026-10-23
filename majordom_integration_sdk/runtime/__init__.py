"""The runtime: radios, firmware, and the services and in-process providers between them and integrations (the
``runtime`` extra; the Bluetooth router's adapters need the ``ble`` extra, DNS-SD the ``discovery`` one).

`Runtime` finds sticks, identifies their firmware, binds every integration's needs to what radios, services, the host,
its own providers (the Bluetooth router, DNS-SD, SSDP) and the integrations themselves offer, and runs the bound
services through Docker Compose.
"""

from majordom_integration_sdk.runtime.peripherals.hardware.drivers import DRIVERS, RadioDriver
from majordom_integration_sdk.runtime.peripherals.providers import Provider, builtin_providers
from majordom_integration_sdk.runtime.peripherals.services.builtin import SERVICES, ThreadNetwork
from majordom_integration_sdk.runtime.runtime import Consumer, PlatformSettings, Radio, Runtime

__all__ = [
    "Runtime",
    "Radio",
    "Consumer",
    "PlatformSettings",
    "Provider",
    "builtin_providers",
    "RadioDriver",
    "DRIVERS",
    "SERVICES",
    "ThreadNetwork",
]
