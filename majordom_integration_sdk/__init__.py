"""MajorDom Integration SDK — models, protocols, and tooling for building integrations.

Public submodules:

- ``majordom_integration_sdk.schemas``       — the wire protocol (pydantic models shared with the Hub)
- ``majordom_integration_sdk.controller``    — ``AbstractController`` + its ``ControllerOutput``
- ``majordom_integration_sdk.platform``      — ``Platform``: the host every peripheral runs on, as ``self.platform``
- ``majordom_integration_sdk.peripherals``   — ``Peripheral``, ``Need``/``Endpoint``, ``Service``: what runs around it
- ``majordom_integration_sdk.capabilities``  — the contracts of in-process peripherals: BLE router, DNS-SD, SSDP
- ``majordom_integration_sdk.repository``    — ``DeviceRepositoryProtocol`` + in-memory / SQLite implementations
- ``majordom_integration_sdk.testing``       — shared test doubles for integration test suites
- ``majordom_integration_sdk.dev``           — standalone runner for developing an integration without the Hub
- ``majordom_integration_sdk.runtime``       — runs peripherals and binds their needs (the ``runtime`` extra)
- ``majordom_integration_sdk.errors``        — errors an integration raises for the Hub to act on
- ``majordom_integration_sdk.files``         — ``read_json`` / ``write_json``: files kept whole, for their owner alone

The names an integration author reaches for most are re-exported here for convenience, e.g.
``from majordom_integration_sdk import AbstractController, Device``.
"""

from importlib.metadata import version

from majordom_integration_sdk.controller import AbstractController, ControllerOutput, IntegrationError
from majordom_integration_sdk.errors import MissingCredentialsError
from majordom_integration_sdk.peripherals import (
    STABLE_RUN,
    Endpoint,
    Need,
    NoSettings,
    Peripheral,
    PeripheralChange,
    PeripheralOutput,
    Peripherals,
    Service,
    UsbDriver,
)
from majordom_integration_sdk.platform import Platform
from majordom_integration_sdk.repository import (
    DeviceRepositoryMemory,
    DeviceRepositoryProtocol,
    SqliteDeviceRepository,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.credentials import ProvidedCredentials, WifiCredentials
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter
from majordom_integration_sdk.schemas.event import DeviceParameterChange, Event

__version__ = version("majordom-integration-sdk")

__all__ = [
    "AbstractController",
    "ControllerOutput",
    "IntegrationError",
    "Platform",
    "DeviceRepositoryProtocol",
    "DeviceRepositoryMemory",
    "SqliteDeviceRepository",
    "DeviceCommand",
    "Device",
    "Discovery",
    "Parameter",
    "ProvidedCredentials",
    "Event",
    "DeviceParameterChange",
    "MissingCredentialsError",
    "Need",
    "Endpoint",
    "Peripheral",
    "NoSettings",
    "PeripheralOutput",
    "Peripherals",
    "Service",
    "UsbDriver",
    "PeripheralChange",
    "WifiCredentials",
    "STABLE_RUN",
]
