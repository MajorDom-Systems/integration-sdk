"""What radios, services, the host and the runtime's own providers offer, and what a consumer gets once bound."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Endpoint:
    """What a radio, a service, the host or an in-process provider offers — and, once bound, what a consumer uses.

    A radio firmware's ``url`` may hold ``{device}``, the stick's path; a service's ``{ports.<name>}``.
    ``params`` says how to talk to it, e.g. a serial port's ``baud`` and ``flow_control`` as the radio's
    firmware sets them; a service reads them as ``{peripherals.<family>.params[baud]}``.

    ``instance`` carries an in-process peripheral itself (the runtime's Bluetooth router, a DNS-SD browser, an
    ESPHome node's Bluetooth source); ``url`` still names it, and two endpoints are the same when all but the
    instance match.
    """

    family: str
    dialect: str = ""
    url: str = ""
    shared: bool = False  # several consumers may bind it (one Thread network); otherwise exactly one
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)
    instance: Any = field(default=None, hash=False, compare=False, repr=False)


type Bound = Endpoint | list[Endpoint]  # what a need is bound to: a list for a ``many`` need


class Peripherals(dict[str, Bound]):
    """A consumer's bound needs by family: an `Endpoint`, or a list of them for a ``many`` need."""

    def endpoint(self, family: str) -> Endpoint:
        """The endpoint bound to the need of ``family``; KeyError while it is unbound."""
        bound = self[family]
        if isinstance(bound, list):
            raise TypeError(f"The {family} need binds many endpoints: use endpoints({family!r})")
        return bound

    def endpoints(self, family: str) -> list[Endpoint]:
        """Every endpoint bound to the need of ``family``; empty while it is unbound."""
        bound = self.get(family, [])
        return bound if isinstance(bound, list) else [bound]

    def instance(self, family: str) -> Any:
        """The in-process peripheral bound to the need of ``family``, e.g. the Bluetooth router; None while unbound."""
        return self.endpoint(family).instance if family in self else None


@dataclass(frozen=True)
class PeripheralChange:
    """A bound need moved: ``after`` is None when it was lost, ``before`` is None when it (re)appeared."""

    family: str
    before: Bound | None
    after: Bound | None
