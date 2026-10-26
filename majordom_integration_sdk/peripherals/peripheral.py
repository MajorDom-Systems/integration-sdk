"""The one protocol for everything around an integration, and how a peripheral talks to whatever runs it."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, ConfigDict

from majordom_integration_sdk.peripherals.endpoint import Bound, Endpoint, PeripheralChange
from majordom_integration_sdk.peripherals.need import Need

if TYPE_CHECKING:
    from majordom_integration_sdk.controller.platform import Platform

log = logging.getLogger(__name__)

START_TIMEOUT = 600  # seconds a start may take to serve by default, else it failed


class NoSettings(BaseModel):
    """The settings of a peripheral without options: it takes none."""

    model_config = ConfigDict(extra="forbid")


class PeripheralOutput(Protocol):
    """What a peripheral tells whatever runs it, through its ``output``, delegate style like `ControllerOutput`: what
    changed on its own (a stick plugged in, a node connected), what it learned, and that it stopped or failed. The
    runtime gives every peripheral it runs the same output; a controller reports through `ControllerOutput` instead.

    Awaiting a call returns once the runtime acted on it, e.g. stopped what used a radio about to be flashed. From
    inside ``start``, ``stop`` or a hook it returns at once: the runtime acts when that call returns. It acts in a task
    of its own, so a call from a task of the peripheral's, which its ``stop`` cancels, is fine: that task just ends.
    """

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        """Its ``provides`` or the peripherals it brings along changed."""
        ...

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        """`Platform` facts it read from what it runs or talks to, e.g. a border router's Thread network: they replace
        the platform's, and every peripheral that runs hears it (``platform_facts_did_change``)."""
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


class LoggingPeripheralOutput:
    """Nothing runs the peripheral (library mode, a test): what it tells is logged."""

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        log.info(f"{peripheral.key} changed what it provides")

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        log.info(f"{peripheral.key} learned {', '.join(facts)}")

    async def peripheral_did_stop(self, peripheral: Peripheral):
        log.info(f"{peripheral.key} stopped by itself")

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        log.error(f"{peripheral.key} failed: {reason}")


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


class Peripheral[S: BaseModel = Any](Protocol):
    """Anything around an integration that needs and/or provides capabilities: a USB radio, soldered hardware, the
    host's Bluetooth adapters and network, a container, an in-process provider (the Bluetooth router, Zeroconf, SSDP),
    an integration itself, an ESPHome node's Bluetooth proxy. Only the construction differs.

    It ``needs`` capabilities, ``provides`` endpoints and the peripherals it brings along, and finds what its needs
    are bound to in ``peripherals``. A runtime (`majordom_integration_sdk.runtime.Runtime`) runs every peripheral it is
    given by one rule: one that isn't disabled or failed runs while whatever brought it along does, while something
    that runs binds it if it ``is_on_demand``, and once every required need is bound to a provider that runs. A
    required need that can't be bound keeps it waiting, with the reason; one lost later stops it until it is back. An
    optional need is handed over once its provider runs, as it appears.

    Subclass it for the defaults. The runtime sets ``platform`` (the host, the same for every peripheral: its facts,
    and a documents folder per peripheral), ``output`` (what it tells), ``peripherals``, ``contributor`` and
    ``settings``, its options validated by its ``Settings``. Declare them as a nested ``class Settings(BaseModel)``
    named as the type parameter, which types ``settings``: ``class Radio(Peripheral["Radio.Settings"])``. Without,
    they are `NoSettings`; a bare ``Peripheral`` is one of any settings. A field marked read-only in its JSON schema,
    ``channel: int = Field(11, json_schema_extra={"readOnly": True})``, shows what the peripheral fills in itself
    (``self.settings.channel = 25``): the user sees it, and an update to another value is refused. It is never kept:
    until the peripheral fills it in again, it holds its default.
    """

    name: str  # what it is shown by
    contributor: Peripheral | None = None  # what brought it along
    needs: Sequence[Need] = ()
    is_on_demand: bool = False  # runs only while something that runs binds it: containers, in-process providers
    start_timeout: float = START_TIMEOUT
    settings: S
    platform: Platform  # the host it runs on
    output: PeripheralOutput = LoggingPeripheralOutput()  # what it tells whatever runs it
    peripherals: Peripherals = Peripherals()  # what its needs are bound to

    @property
    def Settings(self) -> type[S]:
        """The model of its options; `NoSettings` when it has none. Read-only, so a nested class may override it."""
        return cast(type[S], NoSettings)

    @property
    def key(self) -> str:
        """What it is known, kept and disabled by where it runs, unique there and stable across restarts: its name,
        after its contributor's key for one brought along (``usb/<stick>``, ``esphome/<node>``)."""
        return f"{self.contributor.key}/{self.name}" if self.contributor else self.name

    @property
    def provides(self) -> Sequence[Endpoint | Peripheral]:
        """What it serves to the others' needs, and the peripherals it brings along while it runs: the USB scanner a
        radio per stick, ESPHome a Bluetooth proxy per node. When it changes on its own, tell ``output``."""
        return ()

    async def start(self):
        """Set up what it serves, and return once it serves: what needs it starts then. Raising, or taking longer than
        its ``start_timeout``, has the runtime stop it, try again later (``RETRY_DELAYS``), then give up."""
        return

    async def stop(self):
        """Release everything it holds; what it brought along goes with it. It also follows a start that failed or was
        cut short: leave the object clean for the next ``start``, with nothing in flight taking resources after it."""
        return

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """A bound need was lost, appeared or moved while it runs; its ``peripherals`` already hold the new binding.

        Return True when handled in place (Matter keeps working over IP without Thread); False, the default, restarts
        it. A required need that is lost stops it instead, without asking.
        """
        return False

    async def settings_did_change(self, before: S) -> bool:
        """Its ``settings`` changed while it runs. True when applied in place; False, the default, restarts it."""
        return False

    async def platform_facts_did_change(self, before: Platform) -> bool:
        """The host's facts changed (region, Wi-Fi, Thread network) while it runs; ``platform`` holds the new ones.
        True, the default, when they don't concern it or were applied in place; False restarts it."""
        return True
