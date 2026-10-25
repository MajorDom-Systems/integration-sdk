"""The one protocol for everything around an integration, and how a peripheral talks to whatever runs it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from majordom_integration_sdk.peripherals.endpoint import Bound, Endpoint, PeripheralChange
from majordom_integration_sdk.peripherals.need import Need

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.platform import Context


class PeripheralOutput(Protocol):
    """What a peripheral tells whatever runs it, as its ``runtime``, delegate style like `ControllerOutput`: what
    changed on its own (a stick plugged in, a node connected), and that it stopped.

    Awaiting a call returns once the runtime acted on it, e.g. stopped what used a radio about to be flashed. From
    inside ``start``, ``stop`` or a hook it returns at once: the runtime acts when that call returns.
    """

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        """Its ``provides`` or the peripherals it brings along changed."""
        ...

    async def peripheral_did_stop(self, peripheral: Peripheral):
        """It stopped by itself, gracefully: it is stopped (``stop`` releases what it holds) and waits until one of its
        needs changes (goes and comes back, or moves) or the user restarts it."""
        ...

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        """It can't go on, ``reason`` saying why to the user: it is stopped and started again after each of the
        runtime's retry delays, then failed until the user restarts it. Told from inside ``start``, that start
        fails."""
        ...


class _NoRuntime:
    """Nothing runs the peripheral: library mode, a test."""

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        pass

    async def peripheral_did_stop(self, peripheral: Peripheral):
        pass

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        pass


class Peripherals:
    """What a peripheral's needs are bound to, by family: an `Endpoint`, or a list of them for an ``is_many`` need.

    A runtime hands a new one over before ``start`` and whenever the bindings change; in library mode hand it yours:
    ``controller.peripherals = Peripherals({"mdns": zeroconf.endpoint})``.
    """

    def __init__(self, bound: Mapping[str, Bound] | None = None):
        self.bound: Mapping[str, Bound] = MappingProxyType(dict(bound or {}))

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


class Peripheral(Protocol):
    """Anything around an integration that needs and/or provides capabilities: a USB radio, soldered hardware, the
    host's Bluetooth adapters and network, a container, an in-process provider (the Bluetooth router, Zeroconf, SSDP),
    an integration itself, an ESPHome node's Bluetooth proxy. Only the construction differs.

    It ``needs`` capabilities, ``provides`` endpoints and the peripherals it brings along, and finds what its needs
    are bound to in ``peripherals``. A runtime (`majordom_integration_sdk.runtime.Runtime`) runs every peripheral it is
    given by one rule: one that isn't disabled or failed runs while whatever brought it along does, while something
    that runs binds it if it ``is_on_demand``, and once every required need is bound to a provider that runs. A
    required need that can't be bound keeps it waiting, with the reason; one lost later stops it until it is back.

    Subclass it for the defaults. The runtime sets ``runtime``, ``peripherals``, and ``settings`` when the peripheral
    declares its options as a nested ``class Settings(BaseModel)``: annotate ``settings: Settings`` to type it.
    """

    name: str  # unique among the peripherals of a runtime: what it is known, persisted and disabled by
    needs: Sequence[Need] = ()
    is_on_demand: bool = False  # runs only while something that runs binds it: containers, in-process providers
    Settings: Any = None  # Any: ty forbids a subclass to narrow a typed one (to its nested Settings)
    settings: Any = None
    runtime: PeripheralOutput = _NoRuntime()  # what runs it
    peripherals: Peripherals = Peripherals()  # what its needs are bound to

    @property
    def provides(self) -> Sequence[Endpoint | Peripheral]:
        """What it serves to the others' needs, and the peripherals it brings along while it runs: the USB scanner a
        radio per stick, ESPHome a Bluetooth proxy per node. When it changes on its own, tell ``runtime``."""
        return ()

    async def start(self):
        """Set up what it serves, and return once it serves: what needs it starts then. Raising, or taking longer than
        the runtime's ``START_TIMEOUT``, has the runtime stop it, try again later (``RETRY_DELAYS``), then give up."""
        return

    async def stop(self):
        """Release everything it holds; what it brought along goes with it."""
        return

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """A bound need was lost, appeared or moved while it runs; its ``peripherals`` already hold the new binding.

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
