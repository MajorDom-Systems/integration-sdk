"""How a device is paired and set up: the credentials the user gives, and the Wi-Fi the Hub provisions it onto."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from pydantic import Field

from .base import Base
from .settings import SettingsExtraField as Extra


class CredentialsType(str, Enum):
    code = "code"  # e.g. 1234-123-1234 (matter) or 123-45-678 (homekit) # TODO: rename to pin
    qr = "qr"  # raw qr data; TODO: decode to see what data is inside some protocols
    secret = "secret"  # AES key like in esphome
    # The user physically confirms on the device (ESPHome Improv authorizer, a Matter/HomeKit
    # setup button). A confirmation step that carries no value — unlike `none`, which means
    # pairing needs nothing from the user at all.
    button = "button"
    none = "none"  # like yeelight classic LAN

    def with_mask(self, mask: str, hint: str | None = None) -> CredentialsOption:
        """
        Returns a CredentialsOption of this type with the mask (and optionally a hint).
        mask format: D as digit placeholder, other symbols remain unchanged e.g. dashes,
        for example "DDD-DD-DDD" for "123-45-678"
        """
        return CredentialsOption(type=self, mask=mask, hint=hint)

    def with_hint(self, hint: str) -> CredentialsOption:
        """
        Returns a CredentialsOption of this type with the hint: a short UI prompt,
        for example "Press the button on the device"
        """
        return CredentialsOption(type=self, hint=hint)


class CredentialsOption(Base):
    """One way a discovered device can be paired, with what the UI needs to ask for it."""

    type: CredentialsType
    # D is a digit placeholder, other symbols stay as they are: "DDD-DD-DDD" for "123-45-678"
    mask: str | None = None
    # a short UI prompt, e.g. "Press the button on the device" or "Printed on the label"
    hint: str | None = None

    def __eq__(self, other: object) -> bool:
        # Compat: integrations check `credentials.type in discovery.expected_credentials_options`;
        # a bare type (or its str value) matches the option of that type.
        if isinstance(other, str):
            return self.type == other
        return super().__eq__(other)


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


@dataclass(frozen=True)
class WifiCredentials:
    ssid: str
    # the user writes it, nothing reads it back: settings holding a network never return it
    password: Annotated[str, Field(json_schema_extra={Extra.write_only: True})] = ""
