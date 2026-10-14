"""Errors an integration raises for the Hub (and through it the app) to act on."""

import re
from typing import ClassVar, Final, Self

_TYPE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_PREFIX: Final = "Missing credentials: "


class MissingCredentialsError(LookupError):
    """The Hub lacks credentials of ``type`` to pair a device, e.g. a Hub on Ethernet has no Wi-Fi to give a node.

    Raised before the device is touched, with ``str(error)`` as the discovery's ``last_error``, so the app can ask the
    user for them and pair again. ``type`` is a lower_snake_case string kept open for integrations: the known ones are
    the class constants below; an integration documents any custom one it raises. The app maps it to an enum.
    """

    WIFI: ClassVar[str] = "wifi"
    THREAD: ClassVar[str] = "thread"

    def __init__(self, type: str = WIFI) -> None:
        if not _TYPE.fullmatch(type):
            raise ValueError(f"Credentials type must be lower_snake_case, got {type!r}")
        self.type = type
        super().__init__(f"{_PREFIX}{type}")

    @classmethod
    def parse(cls, last_error: str | None) -> Self | None:
        """The error a ``last_error`` was set from, or None if it was set from anything else."""
        if last_error is None or not last_error.startswith(_PREFIX):
            return None
        type_ = last_error.removeprefix(_PREFIX)
        return cls(type_) if _TYPE.fullmatch(type_) else None
