"""Internal record helpers shared by the SDK's repository implementations.

A stored record is one JSON-safe dict per device holding *everything* known about it —
info, ``integration_data`` (from a :class:`Device`) and ``parameters`` (from a
:class:`DeviceState`). ``Device`` and ``DeviceState`` are siblings off ``DeviceInfo``, so
neither alone is a complete record; merging keeps both halves and lets a read deserialize
into whichever view (``as_``) the caller asks for.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from majordom_integration_sdk.schemas.device import Device, DeviceState

type Record = dict[str, Any]


def dump(device: BaseModel) -> Record:
    """JSON-safe dump, so records round-trip identically through memory or SQLite.

    Goes through ``model_dump_json`` rather than ``model_dump(mode="json")`` — the JSON
    serializer is the one these models are actually exercised against (``ParameterState``
    base64-encodes its ``bytes`` value there), so it's what the SQLite rows already store.
    """
    return json.loads(device.model_dump_json())


def merge(base: Record | None, device: Device | DeviceState) -> Record:
    """Merge a device into an existing record without clobbering the other view's fields."""
    return {**(base or {}), **dump(device)}


__all__ = ["Record", "dump", "merge"]
