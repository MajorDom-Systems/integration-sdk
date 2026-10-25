"""What peripherals provide, and what a need is bound to."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Endpoint:
    """What a peripheral provides — a radio, a service, the host, an in-process provider — and, once bound, what the
    peripheral that needs it uses.

    A radio firmware's ``url`` may hold ``{device}``, the stick's path; a service's ``{ports.<name>}``.
    ``params`` says how to talk to it, e.g. a serial port's ``baud`` and ``flow_control`` as the radio's
    firmware sets them; a service reads them as ``{peripherals.<family>.params[baud]}``. The params a service
    provides render like its compose, telling its consumers what it got: ``{"adapter": "{peripherals.ble.url}"}``.

    ``instance`` carries an in-process peripheral itself (the runtime's Bluetooth router, a DNS-SD browser, an
    ESPHome node's Bluetooth source); ``url`` still names it, and two endpoints are the same when all but the
    instance match.
    """

    family: str
    dialect: str = ""
    url: str = ""
    is_shared: bool = False  # several consumers may bind it (one Thread network); otherwise exactly one
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)
    instance: Any = field(default=None, hash=False, compare=False, repr=False)


type Bound = Endpoint | list[Endpoint]  # what a need is bound to: a list for an ``is_many`` need


@dataclass(frozen=True)
class PeripheralChange:
    """A bound need moved: ``after`` is None when it was lost, ``before`` is None when it (re)appeared."""

    family: str
    before: Bound | None
    after: Bound | None
