"""The language integrations and the runtime share to describe what they need and what they offer.

A capability is ``family/dialect``: ``zigbee/ezsp``, ``thread/spinel``, ``matter/matter-server``, ``ble/router``.
Controllers, services and the runtime's own providers declare `Need`s; radios, services, the host and the providers
offer `Endpoint`s. A runtime (`majordom_integration_sdk.runtime`, or you by hand in library mode) binds them and hands
every consumer its `Peripherals`: an `Endpoint` per family, a list for an ``is_many`` need.
"""

from majordom_integration_sdk.peripherals.endpoint import Bound, Endpoint, PeripheralChange, Peripherals
from majordom_integration_sdk.peripherals.need import Need
from majordom_integration_sdk.peripherals.service import Service

__all__ = ["Need", "Endpoint", "Bound", "Peripherals", "PeripheralChange", "Service"]
