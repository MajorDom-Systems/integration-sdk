from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field, SerializeAsAny

from .base import Base, NonEmptyStr
from .parameter import Parameter, ParameterState

# TODO: split the file

# Pairing


class CredentialsType(str, Enum):
    code = "code"  # e.g. 1234-123-1234 (matter) or 123-45-678 (homekit) # TODO: rename to pin
    qr = "qr"  # raw qr data; TODO: decode to see what data is inside some protocols
    secret = "secret"  # AES key like in esphome
    none = "none"  # like yeelight classic LAN

    def with_mask(self, code_mask: str) -> CredentialsType:
        """
        mask format: D as digit placeholder, other symbols remain unchanged e.g. dashes,
        for example "DDD-DD-DDD" for "123-45-678"
        """
        self.code_mask = code_mask
        return self


# FUTURE:
# @dataclass
# class CredentialsCode:
#     code: str

# @dataclass
# class CredentialsSecret:
#     secret: str

# Credentials = Union[CredentialsCode]

type CredentialsValue = str


class ProvidedCredentials(Base):
    """What a pairing request actually supplies — paired with its type, so pairing can
    validate it against Discovery.expected_credentials_options instead of trusting
    whatever the integration guessed at discovery time."""

    type: CredentialsType
    value: CredentialsValue | None = None


class Discovery(Base):
    # technical
    id: UUID
    integration: NonEmptyStr
    expected_credentials_options: list[CredentialsType]  # TODO: pass code mask and other things
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
    main_parameter: UUID | None = None  # for the tap action on the room view, toggle in most cases

    last_seen: datetime | None = None
    available: bool = False
    last_error: str | None = None
    # model_id: UUID | None = None
    # model: DeviceModel - is it needed here?
    # merlin24: DeviceMerlin24 | None # DEPRECATED


class Device(DeviceInfo):  # TODO: review models
    integration_data: SerializeAsAny[dict | Base] = Field(default_factory=Base)


class DeviceDataModel(DeviceInfo):
    parameters: list[Parameter]


class DeviceState(DeviceInfo):
    parameters: list[ParameterState]

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
