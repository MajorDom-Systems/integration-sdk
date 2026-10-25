"""Companion servers an integration brings along, run as containers while something uses them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from majordom_integration_sdk.peripherals.endpoint import Endpoint
from majordom_integration_sdk.peripherals.need import Need
from majordom_integration_sdk.peripherals.peripheral import Peripheral

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.platform import Context


class ServiceHost(Protocol):
    """What runs a `Service`'s container: the runtime's Docker Compose."""

    def provides(self, service: Service) -> Sequence[Endpoint]:
        """Its ``provides`` rendered: the host ports it got, the params it renders."""
        ...

    async def up(self, service: Service):
        """Run its container; return once it serves."""
        ...

    async def down(self, service: Service): ...

    def is_current(self, service: Service) -> bool:
        """Whether its container still runs what it would render now."""
        ...


class _NoHost:
    def provides(self, service: Service) -> Sequence[Endpoint]:
        return service.templates

    async def up(self, service: Service):
        raise NotImplementedError(f"Nothing runs containers here: run {service.name} yourself and pin what it provides")

    async def down(self, service: Service):
        pass

    def is_current(self, service: Service) -> bool:
        return True


class Service(Peripheral):
    """A companion server an integration provides, such as Matter's matterjs-server: a container that runs only while
    something that runs is bound to one of its endpoints. The integration lists it next to its controller, which
    `Need`s what it provides.

    ``compose`` is its Docker Compose service definition (image, environment, volumes, …); its strings may hold
    ``{data}`` (its own data folder), ``{peripherals.<family>.url}``, ``{settings.<field>}``, ``{context.<field>}``,
    ``{host.interface|uid|gid}`` and ``{ports.<name>}``. A format spec turns a value into text: ``{x:!text}`` renders
    ``text`` when ``x`` is empty, ``{x:?text}`` when it isn't; a list item that renders empty is left out, so
    ``"volumes": ["{peripherals.ble.url:?/run/dbus:/run/dbus:ro}"]`` mounts D-Bus only while an adapter is bound.
    The ``params`` of the ``provides`` endpoints render the same way, which is how a consumer learns what its service
    got: ``params={"adapter": "{peripherals.ble.url}"}``. ``settings`` is the pydantic model of its options.

    ``ports`` names the host ports it listens on, each with the port it *prefers*, not a fixed one: the runtime uses it
    when free, else assigns another (45800-45999), keeps the choice across restarts, and fills ``{ports.<name>}`` in
    ``compose`` and in the ``provides`` URLs. Integrations never read a port: they connect through
    ``self.peripherals.endpoint(<family>).url``.

    Its start returns once its container runs, its ports answer, `provision` finished and `is_healthy` says so.
    Subclass it to hook in: `provision` sets up what it runs, `is_healthy` checks more; a ``start`` of your own may tell
    what it learned once it serves (``self.runtime.peripheral_did_learn``).
    """

    is_on_demand = True

    def __init__(
        self,
        name: str,
        compose: Mapping[str, Any],
        needs: Sequence[Need] = (),
        provides: Sequence[Endpoint] = (),
        settings: type[BaseModel] | None = None,
        ports: Mapping[str, int] = {},  # name -> PREFERRED host port; see the docstring
    ):
        self.name, self.compose, self.needs, self.templates = name, compose, needs, provides
        self.Settings, self.ports = settings, ports
        self.host: ServiceHost = _NoHost()  # the runtime's

    @property
    def provides(self) -> Sequence[Endpoint]:
        return self.host.provides(self)

    async def start(self):
        await self.host.up(self)

    async def stop(self):
        await self.host.down(self)

    async def context_did_change(self, before: Context) -> bool:
        return self.host.is_current(self)

    async def provision(self, endpoint: Endpoint):
        """Set up one of its endpoints once it answers, e.g. form a network. Called each time its container is
        (re)created, cancelled when it stops: keep it idempotent."""
        return

    async def is_healthy(self) -> bool:
        """A check of its own on top of its ports answering; asked until it says yes."""
        return True

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"
