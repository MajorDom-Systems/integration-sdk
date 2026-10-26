"""The language integrations and the runtime share to describe what they need and what they provide.

Everything around an integration, and the integration itself, is a `Peripheral`: it ``needs`` capabilities, it
``provides`` `Endpoint`s and the peripherals it brings along. A capability is ``family/dialect``: ``zigbee/ezsp``,
``thread/spinel``, ``matter/matter-server``, ``ble/router``. A runtime (`majordom_integration_sdk.runtime`, or you by
hand in library mode) binds every `Need` to an endpoint and hands it over as the peripheral's ``peripherals``: an
`Endpoint` per family, a list for an ``is_many`` need.
"""

from majordom_integration_sdk.peripherals.ble_proxy import BLEProxy
from majordom_integration_sdk.peripherals.endpoint import Bound, Endpoint, PeripheralChange
from majordom_integration_sdk.peripherals.need import Need
from majordom_integration_sdk.peripherals.peripheral import NoSettings, Peripheral, PeripheralOutput, Peripherals
from majordom_integration_sdk.peripherals.service import Service, ServiceHost
from majordom_integration_sdk.peripherals.usb import USB_DRIVER, UsbDriver

__all__ = [
    "Need",
    "Endpoint",
    "Bound",
    "Peripheral",
    "NoSettings",
    "PeripheralOutput",
    "Peripherals",
    "PeripheralChange",
    "Service",
    "ServiceHost",
    "UsbDriver",
    "USB_DRIVER",
    "BLEProxy",
]
