"""Errors an integration raises for the Hub (and through it the app) to act on."""

import re
from typing import ClassVar
from uuid import UUID

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


class DeviceIdTakenError(Exception):
    """Another device record already holds ``device_id``, the id a pairing would give its device.

    Raised by `AbstractController.check_device_id` before anything irreversible is done to the device; the Hub refuses
    the pairing with it and does not roll it back (nothing ran). It carries the record found: its ``name`` and whether
    it ``is_paired`` (an unpaired one is a record kept after its device was removed), so the app can offer to pair onto
    that record instead.
    """

    def __init__(self, device_id: UUID, name: str, is_paired: bool):
        self.device_id = device_id
        self.name = name
        self.is_paired = is_paired
        super().__init__(f"Device id {device_id} is taken by {name!r} ({'paired' if is_paired else 'unpaired'})")
