from __future__ import annotations

from datetime import datetime
from types import NoneType
from typing import Any, get_args
from uuid import UUID

from pydantic import Field, SerializeAsAny, field_validator

from .base import Base, NonEmptyStr
from .credentials import CredentialsOption, CredentialsType, CredentialsValue, ProvidedCredentials  # noqa: F401  (they lived here)
from .parameter import Parameter, ParameterState

# TODO: split the file

# Pairing


class Discovery(Base):
    # technical
    id: UUID
    integration: NonEmptyStr
    expected_credentials_options: list[CredentialsOption]
    expiration: datetime | None = None
    # for UX
    transport: NonEmptyStr
    device_manufacturer: str | None
    device_name: NonEmptyStr
    device_category: str | None
    device_icon: str | None  # TODO: icon system
    last_error: str | None = None
    # device_model_id: UUID | None = None # is it still relevant?
    # TODO: room hint?
    # TODO: integration_data

    # TODO: handle devices with multiple transports or multiple integrations, show options, add priority, allow choosing

    @field_validator("expected_credentials_options", mode="before")
    @classmethod
    def _accept_bare_types(cls, options: Any) -> Any:
        # stored discoveries and older integrations pass bare types: ["code"] / [CredentialsType.code]
        if not isinstance(options, list):
            return options
        return [{"type": option} if isinstance(option, str) else option for option in options]

    def accepts(self, credentials: ProvidedCredentials | None) -> bool:
        """Whether `credentials` match one of the advertised options; no credentials count as `none`."""
        provided = credentials.type if credentials else CredentialsType.none
        return any(option.type == provided for option in self.expected_credentials_options)


# Device


class DevicePatch(Base):
    name: str
    note: str = ""
    icon: str | None = None
    category: str | None = None
    room_id: UUID
    main_parameter: UUID | None = None


class DeviceCreate(DevicePatch):
    discovery_id: UUID  # | None = None
    credentials: ProvidedCredentials | None = None


class DevicePair(Base):
    """Re-pair discovery to the existing device."""

    device_id: UUID
    discovery_id: UUID
    credentials: ProvidedCredentials | None = None


class DeviceInfo(DevicePatch):
    id: UUID  # NOTE: moved from DeviceCreate, check whether it's correct
    transport: str
    integration: str
    manufacturer: str | None
    # Manufacturer-provided, read-only description of the device (a model blurb / product name).
    # Integrations set it when the protocol exposes one; the Hub never edits it. (User-editable
    # free text is `note`, on DevicePatch.)
    description: str | None = None
    main_parameter: UUID | None = None  # for the tap action on the room view, toggle in most cases

    last_seen: datetime | None = None
    available: bool = False
    last_error: str | None = None
    # model_id: UUID | None = None
    # model: DeviceModel - is it needed here?
    # merlin24: DeviceMerlin24 | None # DEPRECATED


class Device[TData = Any](DeviceInfo):  # TODO: review models
    """A device with what its integration keeps about it, ``integration_data``, typed as ``TData``: a bare ``Device``
    holds any (a dict once read back), ``class HueDevice(Device[HueData])`` its own model. Unset, it is ``TData``
    validated from ``{}`` (the Hub creates a device before its integration pairs it), and stays unset: a save keeps
    the stored one. For ``Device[HueData | None]`` that ``{}``, unset or stored, is None."""

    integration_data: SerializeAsAny[TData] = Field(default_factory=dict, validate_default=True)

    @field_validator("integration_data", mode="before")
    @classmethod
    def _empty_is_none_when_optional(cls, value: Any) -> Any:
        if value == {} and NoneType in get_args(cls.model_fields["integration_data"].annotation):
            return None
        return value


class DeviceDataModel(DeviceInfo):
    parameters: list[Parameter]


class DeviceState[TData = Any, TParameterData = Any](Device[TData]):
    """A device with its parameters' current states, the data of each typed as ``TParameterData``."""

    parameters: list[ParameterState[TParameterData]]

    @property
    def parameters_dict(self):
        return {param.id: param for param in self.parameters}

    def can_set_main_parameter(self, parameter_id: UUID | None) -> bool:
        """Whether `parameter_id` may be this device's main (one-tap) parameter: clearing it
        (None) is always allowed, otherwise it must be one of this device's parameters and
        satisfy ParameterState.can_be_main_parameter."""
        if parameter_id is None:
            return True
        parameter = self.parameters_dict.get(parameter_id)
        return parameter is not None and parameter.can_be_main_parameter


# Device Model TODO: review usage;
# Current idea is to store more metadata that can't be accessed from the device itself,
# for example, manufacturer's website, preview picture, 3d model, specs, etc

# class DeviceModelInfo(Base):
#     id: UUID
#     name: NonEmptyStr
#     transports: list[str]
#     integration: NonEmptyStr
#     manufacturer: str | None
#     category: str | None
#     icon: str | None
#     is_custom: bool = False # TODO: DEPRECATE?
#     # picture: str | None # TODO: resolve

# class DeviceModel(DeviceModelInfo):
#     parameters: list[Parameter]
