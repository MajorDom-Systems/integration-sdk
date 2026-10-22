"""The language integrations and transports share to describe what they need and what they offer.

A capability is ``family/dialect``: ``zigbee/ezsp``, ``thread/spinel``, ``matter/matter-server-ws``. Controllers and
services declare `Need`s; radios and services offer `Endpoint`s. A runtime (majordom-transports, or
you by hand in library mode) binds them and hands every consumer a concrete `Endpoint` per family.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class Endpoint:
    """What a radio, a service or the host offers — and, once bound, what a consumer connects to.

    A radio firmware's ``url`` may hold ``{device}``, the stick's path; a service's ``{ports.<name>}``.
    ``params`` says how to talk to it, e.g. a serial port's ``baud`` and ``flow_control`` as the radio's
    firmware sets them; a service reads them as ``{links.<family>.params[baud]}``.
    """

    family: str
    dialect: str = ""
    url: str = ""
    shared: bool = False  # several consumers may bind it (one Thread network); otherwise exactly one
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Need:
    """What the integration requires from its environment: a dependency something else must provide.

    The runtime binds it to an `Endpoint` and hands that over, to a controller in ``dependencies.links``
    and to a `Service` as ``{links.<family>.url}``.
    """

    family: str
    dialects: Sequence[str] = ()  # empty: any dialect of the family
    required: bool = True

    def accepts(self, endpoint: Endpoint) -> bool:
        return endpoint.family == self.family and (not self.dialects or endpoint.dialect in self.dialects)


@dataclass(frozen=True)
class Service:
    """What the integration provides to its environment: a companion server it brings along, such as
    Matter's matterjs-server. The runtime runs it as a container only while something is bound to one
    of its endpoints.

    ``compose`` is its Docker Compose service definition (image, environment, volumes, …); its strings
    may hold ``{data}``, ``{links.<family>.url}``, ``{settings.<field>}``, ``{context.<field>}``,
    ``{host.interface|uid|gid}`` and ``{ports.<name>}``.

    ``ports`` names the host ports it listens on, each with the port it *prefers*, not a fixed one: the runtime
    uses it when free, else assigns another (45800-45999), keeps the choice across restarts, and fills
    ``{ports.<name>}`` in ``compose`` and in the ``provides`` URLs. Integrations never read a port: they connect
    through ``dependencies.links[<family>].url``.

    Subclass it to hook into the runtime: `provision` sets up what it runs, `learn` reads `Context`
    facts from what it provides.
    """

    name: str
    compose: Mapping[str, Any]
    needs: Sequence[Need] = ()
    provides: Sequence[Endpoint] = ()
    settings: type[BaseModel] | None = None
    ports: Mapping[str, int] = field(default_factory=dict)  # name -> PREFERRED host port; see the docstring

    async def provision(self, endpoint: Endpoint, settings: BaseModel | None) -> None:
        """Set up one of its bound endpoints once it answers, e.g. form a network. Called each time the
        runtime (re)creates this service, cancelled when it changes again: keep it idempotent."""

    async def learn(self, endpoint: Endpoint) -> Mapping[str, Any]:
        """`Context` fields to read from a bound endpoint of the kind this service provides, once known:
        a border router's Thread network. Also called for such an endpoint someone else runs (pinned)."""
        return {}


class ServiceControl(Protocol):
    """What an integration may do to the services it brings (its ``services``), through the runtime."""

    async def restart(self, service: str) -> None:
        """Restart one of them, e.g. after reconfiguring it. The integration keeps running: reconnect."""
        ...


@dataclass(frozen=True)
class NoServiceControl:
    """No runtime runs services here: library mode, the standalone runner."""

    async def restart(self, service: str) -> None:
        raise NotImplementedError(f"Nothing runs {service} here: restart it yourself")


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
