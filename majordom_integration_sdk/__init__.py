"""MajorDom Integration SDK — models, protocols, and tooling for building integrations.

Public submodules:

- ``majordom_integration_sdk.schemas``     — the wire protocol (pydantic models shared with the Hub)
- ``majordom_integration_sdk.controller``  — the ``AbstractController`` framework + ``ControllerOutput``
- ``majordom_integration_sdk.repository``  — ``DeviceRepositoryProtocol`` + in-memory / SQLite implementations
- ``majordom_integration_sdk.discovery``   — zeroconf / SSDP / BLE discovery services
- ``majordom_integration_sdk.testing``     — shared test doubles for integration test suites
- ``majordom_integration_sdk.dev``         — standalone runner for developing an integration without the Hub

The names an integration author reaches for most are re-exported here for convenience, e.g.
``from majordom_integration_sdk import AbstractController, Device``.
"""

from majordom_integration_sdk.controller import AbstractController, ControllerOutput
from majordom_integration_sdk.repository import (
    DeviceRepositoryMemory,
    DeviceRepositoryProtocol,
    SqliteDeviceRepository,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.schemas.event import DeviceParameterChange, Event

__version__ = "0.1.0"

__all__ = [
    "AbstractController",
    "ControllerOutput",
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
]
