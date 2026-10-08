"""The language integrations and transports share to describe what they need and what they offer.

A capability is ``family/dialect``: ``zigbee/ezsp``, ``thread/spinel``, ``matter/ws``. Controllers and
services declare `Need`s; radios and services offer `Endpoint`s. A runtime (majordom-transports, or
you by hand in library mode) binds them and hands every consumer a concrete `Endpoint` per family.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Endpoint:
    """What a radio, a service or the host offers — and, once bound, what a consumer connects to.

    A radio firmware's ``url`` may hold ``{device}``, the stick's path; a service's is used as written.
    """

    family: str
    dialect: str = ""
    url: str = ""
    shared: bool = False  # several consumers may bind it (one Thread network); otherwise exactly one


@dataclass(frozen=True)
class Need:
    family: str
    dialects: Sequence[str] = ()  # empty: any dialect of the family
    required: bool = True

    def accepts(self, endpoint: Endpoint) -> bool:
        return endpoint.family == self.family and (not self.dialects or endpoint.dialect in self.dialects)


@dataclass(frozen=True)
class Service:
    """A container the runtime starts only while something is bound to one of its endpoints.

    ``compose`` is its Docker Compose service definition (image, environment, volumes, …); its strings
    may hold ``{data}``, ``{links.<family>.url}``, ``{settings.<field>}``, ``{context.<field>}`` and
    ``{host.interface|uid|gid}``.
    """

    name: str
    compose: Mapping[str, Any]
    needs: Sequence[Need] = ()
    provides: Sequence[Endpoint] = ()
    settings: type[BaseModel] | None = None


@dataclass(frozen=True)
class PeripheralChange:
    """A bound link moved: ``after`` is None when it was lost, ``before`` is None when it (re)appeared."""

    family: str
    before: Endpoint | None
    after: Endpoint | None


@dataclass(frozen=True)
class WifiCredentials:
    ssid: str
    password: str = ""


@dataclass(frozen=True)
class Context:
    """Hub-wide facts an integration may need to set devices up."""

    region: str | None = None  # RF region for sub-GHz radios, a Z-Wave JS name: "Europe", "USA", …
    wifi: WifiCredentials | None = None  # the network to provision Wi-Fi devices onto
    thread_dataset: str | None = None  # active operational dataset of the hub's Thread network, hex TLVs
