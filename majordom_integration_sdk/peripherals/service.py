"""Companion servers an integration brings along, run by the runtime while something uses them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from majordom_integration_sdk.peripherals.endpoint import Endpoint
from majordom_integration_sdk.peripherals.need import Need


@dataclass(frozen=True)
class Service:
    """What the integration provides to its environment: a companion server it brings along, such as
    Matter's matterjs-server. The runtime runs it as a container only while something is bound to one
    of its endpoints.

    ``compose`` is its Docker Compose service definition (image, environment, volumes, …); its strings
    may hold ``{data}``, ``{peripherals.<family>.url}``, ``{settings.<field>}``, ``{context.<field>}``,
    ``{host.interface|uid|gid}`` and ``{ports.<name>}``. A format spec turns a value into text: ``{x:!text}`` renders
    ``text`` when ``x`` is empty, ``{x:?text}`` when it isn't; a list item that renders empty is left out, so
    ``"volumes": ["{peripherals.ble.url:?/run/dbus:/run/dbus:ro}"]`` mounts D-Bus only while an adapter is bound.

    ``ports`` names the host ports it listens on, each with the port it *prefers*, not a fixed one: the runtime
    uses it when free, else assigns another (45800-45999), keeps the choice across restarts, and fills
    ``{ports.<name>}`` in ``compose`` and in the ``provides`` URLs. Integrations never read a port: they connect
    through ``self.peripherals.endpoint(<family>).url``.

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
