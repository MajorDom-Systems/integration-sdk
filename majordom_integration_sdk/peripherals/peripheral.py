"""The one protocol for everything around an integration, and how a peripheral talks to whatever runs it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from majordom_integration_sdk.peripherals.endpoint import Bound, Endpoint, PeripheralChange
from majordom_integration_sdk.peripherals.need import Need

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.platform import Context


class PeripheralOutput(Protocol):
    """Where a peripheral reports what changed on its own (a stick plugged in, a node connected), delegate style like
    `ControllerOutput`; the runtime injects itself as `Peripheral.output`.

    Awaiting a call returns once the runtime acted on it, e.g. stopped what used a radio about to be flashed. From
    inside ``start``, ``stop`` or a hook it returns at once: the runtime reads the change when that call returns.
    """

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        """Its ``provides`` or the peripherals it brings along changed."""
        ...

    async def peripheral_did_become_ready(self, peripheral: Peripheral):
        """It serves now, so what needs it may start; only for one that isn't ``is_ready_on_start``."""
        ...

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        """It stopped by itself and can't go on: it is stopped as failed until the user restarts it."""
        ...


class _NoOutput:
    """Nothing runs the peripheral: library mode, a test."""

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        pass

    async def peripheral_did_become_ready(self, peripheral: Peripheral):
        pass

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        pass


class Peripheral(Protocol):
    """Anything around an integration that needs and/or provides capabilities: a USB radio, soldered hardware, the
    host's Bluetooth adapters and network, a container, an in-process provider (the Bluetooth router, Zeroconf, SSDP),
    an integration itself, an ESPHome node's Bluetooth proxy. Only the construction differs.

    A runtime (`majordom_integration_sdk.runtime.Runtime`) runs every peripheral it is given by one rule: one that
    isn't disabled or failed runs while whatever brought it along does, while something that runs binds it if it
    ``is_on_demand``, and once every required need is bound to a provider that runs and is ready. A required need that
    can't be bound keeps it waiting, with the reason; one lost later stops it until it is back.

    Subclass it for the defaults. The runtime sets ``output``, and ``settings`` when the peripheral declares its options
    as a nested ``class Settings(BaseModel)``: annotate ``settings: Settings`` to type it.
    """

    name: str  # unique among the peripherals of a runtime: what it is known, persisted and disabled by
    needs: Sequence[Need] = ()
    is_on_demand: bool = False  # runs only while something that runs binds it: containers, in-process providers
    is_ready_on_start: bool = True  # False: it serves only once it says so (`peripheral_did_become_ready`)
    Settings: Any = None
    settings: Any = None
    output: PeripheralOutput = _NoOutput()

    @property
    def provides(self) -> Sequence[Endpoint]:
        """What it serves to the others' needs; when it changes on its own, tell ``output``."""
        return ()

    @property
    def peripherals(self) -> Sequence[Peripheral]:
        """The peripherals it brings along (an integration its containers, the USB scanner a radio per stick): they run
        only while it does. Hold a `Peripherals` to also get the endpoints your needs are bound to."""
        return ()

    async def start(self):
        """Set up what it serves. Raising has the runtime try again later (`RETRY_DELAYS`), then give up on it."""
        return

    async def stop(self):
        """Release everything it holds, and let go of what it brought along while it ran."""
        return

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """A bound need was lost, appeared or moved while it runs; its `Peripherals` already hold the new binding.

        Return True when handled in place (Matter keeps working over IP without Thread); False, the default, restarts
        it. A required need that is lost stops it instead, without asking.
        """
        return False

    async def settings_did_change(self, before: Any) -> bool:
        """Its ``settings`` changed while it runs. True when applied in place; False, the default, restarts it."""
        return False

    async def context_did_change(self, before: Context) -> bool:
        """Hub-wide facts changed (region, Wi-Fi, Thread network) while it runs. True, the default, when they don't
        concern it or were applied in place; False restarts it."""
        return True


type Restart = Callable[[Peripheral], Awaitable[None]]


class Peripherals(list[Peripheral]):
    """A peripheral's own peripherals: the ones it brings along, as a list (change it, then tell `Peripheral.output`),
    and the endpoints its needs are bound to by family: an `Endpoint`, or a list of them for an ``is_many`` need.

    A runtime fills in the bound needs before ``start`` and whenever they change; in library mode `bind` them yourself.
    """

    def __init__(self, contributed: Iterable[Peripheral] = (), bound: Mapping[str, Bound] | None = None):
        super().__init__(contributed)
        self.bound: Mapping[str, Bound] = {}
        self._restart: Restart | None = None
        self.bind(bound or {})

    def bind(self, bound: Mapping[str, Bound], restart: Restart | None = None):
        """Hand over the bound needs, and with ``restart`` how to restart what it brought along."""
        self.bound = dict(bound)
        self._restart = restart or self._restart

    def endpoint(self, family: str) -> Endpoint:
        """The endpoint bound to the need of ``family``; KeyError while it is unbound."""
        bound = self.bound[family]
        if isinstance(bound, list):
            raise TypeError(f"The {family} need binds many endpoints: use endpoints({family!r})")
        return bound

    def endpoints(self, family: str) -> list[Endpoint]:
        """Every endpoint bound to the need of ``family``; empty while it is unbound."""
        bound = self.bound.get(family, [])
        return bound if isinstance(bound, list) else [bound]

    def instance(self, family: str) -> Any:
        """The in-process peripheral bound to the need of ``family``, e.g. the Bluetooth router; None while unbound."""
        return self.endpoint(family).instance if family in self.bound else None

    async def restart(self, peripheral: Peripheral):
        """Restart one of the peripherals it brought along, e.g. a container after reconfiguring it; the others, and
        whatever uses it, keep running."""
        if not any(p is peripheral for p in self):
            raise PermissionError(f"{peripheral.name} isn't one it brought along")
        if self._restart is None:
            raise NotImplementedError(f"Nothing runs {peripheral.name} here: restart it yourself")
        await self._restart(peripheral)
