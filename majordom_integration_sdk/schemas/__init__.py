"""The MajorDom wire protocol — the device/parameter models shared between an integration
and the Hub.

Scope is deliberately the device domain: devices, parameters, pairing/discovery, commands,
and parameter-change reports. Hub-only concerns (rooms, houses, users, the websocket
protocol) and the automation event bus live in the Hub, not here.
"""

from .base import Base, NonEmptyStr, StrEnum, StrIdentifiable, UUIdentifable
from .command import DeviceCommand
from .device import (
    CredentialsType,
    CredentialsValue,
    Device,
    DeviceCreate,
    DeviceDataModel,
    DeviceInfo,
    DevicePair,
    DevicePatch,
    DeviceState,
    Discovery,
    ProvidedCredentials,
)
from .event import DeviceParameterChange, Event
from .parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterUnit,
    ParameterVisibility,
    ParameterVisibilityPatch,
)

__all__ = [
    # base
    "Base",
    "NonEmptyStr",
    "StrEnum",
    "StrIdentifiable",
    "UUIdentifable",
    # command / events
    "DeviceCommand",
    "Event",
    "DeviceParameterChange",
    # device
    "CredentialsType",
    "CredentialsValue",
    "Device",
    "DeviceCreate",
    "DeviceDataModel",
    "DeviceInfo",
    "DevicePair",
    "DevicePatch",
    "DeviceState",
    "Discovery",
    "ProvidedCredentials",
    # parameter
    "Parameter",
    "ParameterDataType",
    "ParameterRole",
    "ParameterState",
    "ParameterUnit",
    "ParameterVisibility",
    "ParameterVisibilityPatch",
]
