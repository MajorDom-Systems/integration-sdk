"""Errors an integration raises for the Hub (and through it the app) to act on."""

import re
from typing import ClassVar

_TYPE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


class MissingCredentialsError(LookupError):
    """The Hub lacks credentials of ``type`` to pair a device, e.g. a Hub on Ethernet has no Wi-Fi to give a node.

    Raised before the device is touched; the Hub answers the pairing request with it (422 ``missing_credentials``,
    carrying ``type`` and the kept device id), so the app can ask the user for them and pair again. ``type`` is a
    lower_snake_case string kept open for integrations: the known ones are the class constants below; an integration
    documents any custom one it raises. The app maps it to an enum.
    """

    WIFI: ClassVar[str] = "wifi"
    THREAD: ClassVar[str] = "thread"

    def __init__(self, type: str = WIFI):
        if not _TYPE.fullmatch(type):
            raise ValueError(f"Credentials type must be lower_snake_case, got {type!r}")
        self.type = type
        super().__init__(f"Missing credentials: {type}")
