"""Internal record helpers shared by the SDK's repository implementations.

A stored record is one JSON-safe dict per device holding *everything* known about it —
info, ``integration_data`` and ``parameters`` (from a :class:`DeviceState`). A ``Device``
lacks the parameters, and a ``DeviceState`` built without ``integration_data`` doesn't hold
the stored one; merging keeps both halves and lets a read deserialize into whichever view
(``as_``) the caller asks for.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from majordom_integration_sdk.schemas.device import Device, DeviceState

type Record = dict[str, Any]


def dump(device: BaseModel, exclude: set[str] | None = None) -> Record:
    """JSON-safe dump, so records round-trip identically through memory or SQLite.

    Goes through ``model_dump_json`` rather than ``model_dump(mode="json")`` — the JSON
    serializer is the one these models are actually exercised against (``ParameterState``
    base64-encodes its ``bytes`` value there), so it's what the SQLite rows already store.
    """
    return json.loads(device.model_dump_json(exclude=exclude))


def merge(base: Record | None, device: Device | DeviceState) -> Record:
    """Merge a device into an existing record without clobbering the other view's fields."""
    unset = {"integration_data"} - device.model_fields_set
    return {**(base or {}), **dump(device, exclude=unset)}


__all__ = ["Record", "dump", "merge"]
