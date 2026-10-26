"""Run peripherals by one rule and bind their needs — what the runtime is given and what those bring along — then keep
it in sync as sticks come and go, providers change, and the user enables, disables or restarts them."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from pydantic_core import InitErrorDetails

from majordom_integration_sdk.controller.output import ControllerOutput, LoggingControllerOutput
from majordom_integration_sdk.controller.platform import Platform
from majordom_integration_sdk.peripherals import (
    Bound,
    Endpoint,
    Need,
    Peripheral,
    PeripheralChange,
    Peripherals,
)
from majordom_integration_sdk.runtime.files import read_json, write_json
from majordom_integration_sdk.runtime.output import HostOutput

log = logging.getLogger(__name__)

RETRY_DELAYS = (5, 30, 120)  # seconds before each new start of a peripheral that failed, then it is given up on
STABLE_RUN = 300  # seconds of running that clear the failures in a row
HOOK_TIMEOUT = 30  # seconds a hook may take: it runs while the runtime waits
STOP_TIMEOUT = 15  # a stop stuck on a vanished radio (zigpy awaiting its dead I/O thread) is abandoned
_PASSES = 10  # bound on the passes one change takes to settle: each may change what the next one binds
_FACTS = {f.name for f in fields(Platform)} - {"documents_root", "make_device_repository"}  # never its resources


class PeripheralState(StrEnum):
    DISABLED = "disabled"  # by the user, persisted: until enabled
    WAITING = "waiting"  # for a need, a provider, what brought it along, something to use it, or a retry
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"  # given up on: until restarted or its settings change


class StatusKind(StrEnum):
    """Why a peripheral waits or failed, for its owner to act on; the status's ``detail`` says it in words."""

    NEEDS_UNBOUND = "needs-unbound"  # waiting: a required need can't be bound
    NEEDS_STARTING = "needs-starting"  # waiting: its needs are bound, to providers that don't run yet
    UNUSED = "unused"  # waiting: on demand, and nothing that runs uses it
    RETRYING = "retrying"  # waiting: it failed, and starts again after a delay
    STOPPED_ITSELF = "stopped-itself"  # waiting: until its needs change or the user restarts it
    GAVE_UP = "gave-up"  # failed: its retries are spent
    INVALID_SETTINGS = "invalid-settings"  # failed: its stored settings don't validate


@dataclass(frozen=True)
class PeripheralStatus:
    state: PeripheralState
    kind: StatusKind | None = None  # why it waits or failed
    detail: str = ""  # the same in words, for logs and the CLI

    def __str__(self) -> str:
        return f"{self.state}: {self.detail}" if self.detail else self.state


class RuntimeOutput(Protocol):
    """What the runtime reports to its owner (the Hub), delegate style like `PeripheralOutput`. Called synchronously
    from the runtime's loop, in order: don't block, schedule what takes time."""

    def peripheral_did_change_status(self, peripheral: Peripheral, status: PeripheralStatus): ...

    def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        """`Platform` facts it read from what it runs (a border router's Thread network), which the runtime's platform
        takes now (`Runtime.update_platform`): keep them for the next boot."""
        ...

    def peripheral_did_disappear(self, peripheral: Peripheral):
        """It is no longer run here: what brought it along let go of it (a stick unplugged), or it was removed."""
        ...


class DisabledStore(Protocol):
    """Where the user's disabling of a peripheral is kept, by its ``key``: the Hub's enable/disable store."""

    def is_disabled(self, key: str) -> bool: ...
    def set_disabled(self, key: str, is_disabled: bool): ...


class MemoryDisabledStore:
    """Disabled until the process ends: library mode, the dev runner."""

    def __init__(self, disabled: Iterable[str] = ()):
        self.disabled = set(disabled)

    def is_disabled(self, key: str) -> bool:
        return key in self.disabled

    def set_disabled(self, key: str, is_disabled: bool):
        (self.disabled.add if is_disabled else self.disabled.discard)(key)


_GIVEN_UP = (PeripheralState.DISABLED, PeripheralState.FAILED)

type Binding = tuple[_Entry, Endpoint]  # who provides it, and what
type _Identity = dict[str, list[tuple[_Entry, Endpoint, int]]]  # what tells bindings apart


@dataclass(eq=False)
class _Entry:
    peripheral: Peripheral
    key: str  # what it is known by here
    state: PeripheralState
    kind: StatusKind | None = None
    detail: str = ""
    attempts: int = 0  # failures in a row
    running_since: float | None = None  # monotonic
    retry_at: float = 0.0  # monotonic: a waiting one starts no earlier
    failure: str = ""  # why, and when it is tried again
    bindings: dict[str, list[Binding]] = field(default_factory=dict)  # what it was started or last told with
    stopped_with: _Identity | None = None  # what it was bound to when it stopped by itself: it waits for a change
    told_failure: str = ""  # while starting
    is_stopping_itself: bool = False  # told while starting
    platform_before: Platform | None = None  # what the platform was when it changed while starting: told once it runs
    settings_before: BaseModel | None = None  # the same of its settings
    task: asyncio.Task | None = None  # its start
    reported: PeripheralStatus | None = None

    @property
    def status(self) -> PeripheralStatus:
        return PeripheralStatus(self.state, self.kind, self.detail)

    @property
    def is_started(self) -> bool:
        return self.state in (PeripheralState.STARTING, PeripheralState.RUNNING)


class Runtime:
    """Runs every peripheral it is given, and what those bring along, by one rule (see `Peripheral`); binds their
    needs, and hears what each tells through the ``output`` they share::

        runtime = Runtime(data, [Pinned(*mine), FixedPeripherals(*soldered), *default_peripherals(), *integrations])
        await runtime.start()

    ``peripherals`` bind in the order given, what each brings along right after it: one given first wins a single-use
    endpoint (the Bluetooth router takes every adapter before the containers) and serves a need first: `Pinned`,
    then the hardware, the defaults and the integrations. ``platform`` is the host every peripheral runs on, its
    ``self.platform``: the facts peripherals read and containers render, a documents folder per peripheral (by default
    under ``data/peripherals``) and the device repository; `update_platform` replaces it. ``controller_output`` (the
    Hub's) hears what controllers report through their ``output``, logged by default. ``output`` hears every change of
    status, what is learned and what is gone; ``disabled`` keeps what the user disabled (in memory by default). Its own
    state, the settings, goes to ``data``.

    A peripheral that isn't disabled or failed runs while something that runs binds it if it ``is_on_demand``, while
    what brought it along runs, and once every required need is bound to a provider that runs. Only a waiting one
    starts by itself: a stick plugged in again never revives a disabled or failed one. A start that raises or takes too
    long, a crash it tells (``peripheral_did_fail``) and a hook that raises are tried again after each of
    ``retry_delays`` (`RETRY_DELAYS` unless given: a test passes short ones), then it is failed; one that stops by
    itself (``peripheral_did_stop``) waits for its needs to change. What it brought along goes when it stops. A provider
    restarting is a blip for what runs on it, but for one bound to the very object it runs in process (a zeroconf
    browser): that one stops first and starts again after.
    """

    def __init__(
        self,
        data: Path,
        peripherals: Sequence[Peripheral] = (),
        *,
        platform: Platform | None = None,
        controller_output: ControllerOutput | None = None,
        output: RuntimeOutput | None = None,
        disabled: DisabledStore | None = None,
        retry_delays: Sequence[float] | None = None,
    ):
        self.data, self.given, self.output = data, list(peripherals), output
        self._retry_delays = retry_delays  # None: RETRY_DELAYS, read when it fails (a test may patch it)
        self.platform = platform or Platform(documents_root=data / "peripherals")
        self._shared_output = HostOutput(self, controller_output or LoggingControllerOutput())
        self.disabled = disabled or MemoryDisabledStore()
        self.state: dict[str, Any] = {"settings": {}}
        saved = read_json(data / "state.json")
        self.state |= {key: saved[key] for key in saved.keys() & self.state.keys()}  # older keys start fresh
        self._entries: dict[str, _Entry] = {}  # in binding order
        self._duplicates: list[Peripheral] = []  # left out: another has their key
        self._gone: list[_Entry] = []  # to report
        self._failed: dict[str, str] = {}  # key -> why, for the session: one brought along anew stays failed
        self._wanted: set[str] = set()  # the ones that should run
        self._bound: dict[str, dict[str, list[Binding]]] = {}
        self._unbound: dict[str, dict[str, str]] = {}  # key -> family -> why its need isn't bound
        self._lock = asyncio.Lock()
        self._holder: asyncio.Task | None = None  # the task holding the lock
        self._tasks: set[asyncio.Task] = set()
        self._timer: asyncio.TimerHandle | None = None
        self._is_running = False
        self._collect()  # known, with their status and settings, before it starts

    # Lifecycle

    async def start(self):
        self._is_running = True
        await self.refresh()

    async def stop(self):
        self._is_running = False
        if self._timer:
            self._timer.cancel()
        async with self._locked():
            for entry in _users_first([entry for entry in self._entries.values() if entry.is_started]):
                await self._stop(entry, _waiting())
        for task in self._tasks:
            task.cancel()

    async def refresh(self):
        """Collect, bind and run everything anew now; before it starts, only collect."""
        if not self._is_running:
            self._collect()  # none of them ran: none to stop or report gone
            return
        async with self._locked():
            for _ in range(_PASSES):
                if not await self._pass():
                    break
            else:
                log.warning(f"Peripherals still changing after {_PASSES} passes")
        self._report()
        self._schedule_retry()

    # What the Hub (or you) reads

    @property
    def peripherals(self) -> list[Peripheral]:
        """Every peripheral it runs, in binding order: what it was given and what those brought along. One whose key
        another one before it has is left out, and logged."""
        return [entry.peripheral for entry in self._entries.values()]

    def peripheral(self, key: str) -> Peripheral:
        return self._entries[key].peripheral

    def status(self, peripheral: Peripheral) -> PeripheralStatus:
        return self._entry(peripheral).status

    def bound(self, peripheral: Peripheral) -> dict[str, Bound]:
        """What each need is bound to, by family: an `Endpoint`, a list of them for an ``is_many`` need. What it has
        (or would have) in ``peripherals``: an optional need only once its provider runs."""
        return _view(peripheral, self._held(self._entry(peripheral)))

    def providers(self, peripheral: Peripheral) -> dict[str, str | list[str]]:
        """Who serves each bound need, by family: its key, a list of them for an ``is_many`` need (one per endpoint, as
        `bound` lists them)."""
        many = {need.family for need in peripheral.needs if need.is_many}
        bound = self._held(self._entry(peripheral))
        return {family: [p.key for p, _ in b] if family in many else b[0][0].key for family, b in bound.items()}

    def unbound(self, peripheral: Peripheral) -> dict[str, str]:
        """Why each need isn't bound, by family: for the developer (logs, the CLI), with what to do when there is
        something, e.g. 'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'. The Hub
        words its own message for the user."""
        return dict(self._unbound.get(self._entry(peripheral).key, {}))

    def stored_settings(self, peripheral: Peripheral) -> dict[str, Any]:
        """Its settings as kept, raw: what to show with an invalid-settings failure, where they don't validate."""
        return copy.deepcopy(self.state["settings"].get(self._entry(peripheral).key, {}))

    def users(self, peripheral: Peripheral) -> list[str]:
        """Every peripheral that runs on it, directly or through others: a radio's integrations and their containers."""
        users: list[str] = []
        providers = {self._entry(peripheral).key}
        while True:
            more = [
                key
                for key, bound in self._bound.items()
                if key not in users and any(p.key in providers for b in bound.values() for p, _ in b)
            ]
            if not more:
                return users
            users += more
            providers.update(more)

    def uses(self, peripheral: Peripheral) -> list[str]:
        """Every peripheral it runs on, directly or through others: an integration's containers and radios."""
        used: list[str] = []
        owners = [self._entry(peripheral).key]
        while owners:
            bound = self._bound.get(owners.pop(0), {})
            more = [p.key for b in bound.values() for p, _ in b if p.key not in used]
            used += dict.fromkeys(more)
            owners += more
        return used

    def reach(self, endpoints: Iterable[Endpoint]) -> list[str]:
        """The families ``endpoints`` would serve, themselves or through the on-demand peripherals here they would run,
        that something needs: one that runs anyway, or one used that isn't on the way: what a radio's firmware is good
        for."""
        alive = [entry for entry in self._entries.values() if entry.state not in _GIVEN_UP]
        available = {endpoint.family for entry in alive for endpoint in _offered(entry.peripheral)}
        families: set[str] = set()
        reached, on_the_way = {e.family for e in endpoints}, set[str]()
        while reached != families:
            families = reached
            on_the_way = {
                key
                for key, entry in self._entries.items()
                if entry.peripheral.is_on_demand and _is_fed(entry.peripheral.needs, families, available)
            }
            reached = families.union(
                *({e.family for e in _offered(self._entries[key].peripheral)} for key in on_the_way)
            )
        users = [
            entry.peripheral
            for key, entry in self._entries.items()
            if not entry.peripheral.is_on_demand or (key in self._wanted and key not in on_the_way)
        ]
        return sorted(families & {need.family for peripheral in users for need in peripheral.needs})

    # What the Hub (or you) changes

    async def add(self, peripheral: Peripheral):
        """Run ``peripheral`` too, as if given last: an integration installed while the hub runs. ValueError when
        another peripheral has its key."""
        if any(other.key == peripheral.key for other in [*self.given, *self.peripherals]):
            raise ValueError(f"Can't add {peripheral.name}: another peripheral has the key {peripheral.key}")
        self.given.append(peripheral)
        await self._settle()

    async def remove(self, peripheral: Peripheral):
        """Stop running ``peripheral``, given or added; its owner hears it is gone (``peripheral_did_disappear``)."""
        self.given = [given for given in self.given if given is not peripheral]
        await self._settle()

    async def enable(self, peripheral: Peripheral):
        entry = self._entry(peripheral)
        self.disabled.set_disabled(entry.key, False)

        async def apply():
            if entry.state == PeripheralState.DISABLED:
                entry.state = PeripheralState.WAITING
                self._revive(entry)
                self._check_settings(entry)

        await self._act(apply)

    async def disable(self, peripheral: Peripheral):
        """Stop it, and what it brought along, until enabled again; kept across restarts."""
        entry = self._entry(peripheral)
        self.disabled.set_disabled(entry.key, True)
        await self._act(lambda: self._stop(entry, PeripheralStatus(PeripheralState.DISABLED)))

    async def restart(self, peripheral: Peripheral):
        """Stop it if it runs and start it again, a failed one or one that stopped by itself too: its retries start
        over, and its kept settings are read again (still invalid, it stays failed until they are updated). Called from
        inside a peripheral's own start, stop or hook, it happens once that call returns."""
        entry = self._entry(peripheral)
        if entry.state == PeripheralState.DISABLED:
            raise ValueError(f"{entry.key} is disabled: enable it")

        async def apply():
            await self._stop(entry, _waiting())
            self._revive(entry)
            self._check_settings(entry)

        await self._act(apply)

    async def update_settings[S: BaseModel](self, peripheral: Peripheral[S], values: Mapping[str, Any]) -> S:
        """Validate and keep new values of its ``Settings``, none it doesn't have and no other value of a read-only one
        than its own; one that runs hears it (`settings_did_change`), one still starting once it runs, a failed one or
        one that stopped by itself tries again."""
        entry, model = self._entry(peripheral), peripheral.Settings
        live = getattr(peripheral, "settings", None)  # none yet when the ones kept didn't validate
        read_only = _read_only(model)
        filled = live.model_dump(mode="json", include=read_only) if live else {}  # never kept: only the live ones
        current = self.state["settings"].get(entry.key, {}) | filled
        unknown = sorted(values.keys() - model.model_fields.keys())
        if changed := sorted(k for k in values.keys() & read_only if values[k] != current.get(k)) + unknown:
            refused: list[InitErrorDetails] = [
                {"type": "extra_forbidden" if k in unknown else "frozen_field", "loc": (k,), "input": values[k]}
                for k in changed
            ]
            raise ValidationError.from_exception_data(model.__name__, refused)
        settings = model.model_validate(current | dict(values))
        before = live if live is not None else settings
        peripheral.settings = settings
        self._keep_settings(entry.key, settings)

        async def apply():
            if entry.state in (PeripheralState.FAILED, PeripheralState.WAITING):
                entry.state = PeripheralState.WAITING
                self._revive(entry)
            elif entry.state == PeripheralState.RUNNING:
                await self._ask(entry, peripheral.settings_did_change(before))
            elif entry.state == PeripheralState.STARTING and entry.settings_before is None:  # it may have read them
                entry.settings_before = before

        await self._act(apply)
        return peripheral.settings

    async def update_platform(self, platform: Platform):
        """The host's facts changed: every peripheral reads ``platform`` from now on, built from the one there was
        (``dataclasses.replace(runtime.platform, region="Europe")``); one that runs hears it
        (`Peripheral.platform_facts_did_change`), one still starting may have read the ones before, so it hears it once
        it runs."""
        before, self.platform = self.platform, platform
        for entry in self._entries.values():
            entry.peripheral.platform = platform
        if platform == before:
            return
        started = [entry for entry in self._entries.values() if entry.is_started]  # the ones that read the ones before

        async def apply():
            for entry in started:
                if entry.state == PeripheralState.RUNNING:
                    await self._ask(entry, entry.peripheral.platform_facts_did_change(before))
                elif entry.state == PeripheralState.STARTING and entry.platform_before is None:
                    entry.platform_before = before

        await self._act(apply)

    # What peripherals tell, through their output

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        await self._settle()

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        if self._find(peripheral) is None:
            return
        if unknown := sorted(facts.keys() - _FACTS):  # a newer peripheral's: neither kept nor in the way of the others
            log.warning(f"{peripheral.key} learned what the platform holds no fact of, left out: {', '.join(unknown)}")
        if not (known := {name: value for name, value in facts.items() if name in _FACTS}):
            return
        if self.output:
            with _logged(f"Reporting what {peripheral.name} learned"):
                self.output.peripheral_did_learn(peripheral, known)
        await self.update_platform(replace(self.platform, **known))

    async def peripheral_did_stop(self, peripheral: Peripheral):
        if (entry := self._find(peripheral)) is None:
            return
        if entry.state == PeripheralState.STARTING:
            entry.is_stopping_itself = True
            return

        async def apply():
            if entry.state == PeripheralState.RUNNING:
                await self._stop_itself(entry)

        await self._act(apply)

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        if (entry := self._find(peripheral)) is None:
            return
        if entry.state == PeripheralState.STARTING:
            entry.told_failure = entry.told_failure or reason
            return

        async def apply():
            if entry.state == PeripheralState.RUNNING:
                await self._stop(entry, _waiting())
                self._fail(entry, reason)

        await self._act(apply)

    # Internals

    def _entry(self, peripheral: Peripheral) -> _Entry:
        if (entry := self._find(peripheral)) is None:
            raise LookupError(f"{peripheral.name} isn't run here")
        return entry

    def _find(self, peripheral: Peripheral) -> _Entry | None:
        return next((entry for entry in self._entries.values() if entry.peripheral is peripheral), None)

    async def _act(self, change: Callable[[], Awaitable[Any]]):
        """Make a change and settle."""
        await self._in_own_task(self._act_unlocked(change))

    async def _act_unlocked(self, change: Callable[[], Awaitable[Any]]):
        async with self._locked():
            await change()
        await self.refresh()

    async def _settle(self):
        """Bind and run anew."""
        await self._in_own_task(self.refresh())

    async def _in_own_task(self, work: Awaitable[Any]):
        """Do ``work`` in a task of the runtime's own, never in the caller's: a stop it brings may cancel the caller (a
        peripheral telling from its own task), and a caller cancelled meanwhile only stops waiting. The caller waits
        for it, unless the runtime waits on the caller: then it happens once that call returns."""
        task = self._spawn(work)
        if not self._is_driving():
            await asyncio.shield(task)

    def _is_driving(self) -> bool:
        """Whether the runtime waits on the current task: it holds the lock, or it is a start. Waiting in it for the
        runtime to act would never end; a task it spawned is no one the runtime waits on."""
        task = asyncio.current_task()
        return task is self._holder or any(entry.task is task for entry in self._entries.values())

    @contextlib.asynccontextmanager
    async def _locked(self):
        async with self._lock:
            self._holder = asyncio.current_task()
            try:
                yield
            finally:
                self._holder = None

    async def _pass(self) -> bool:
        """One round: collect what there is, bind it, stop what shouldn't run, tell what runs what moved, start what
        can. True when something changed that the next round may bind differently."""
        before = self._snapshot()
        is_changed = False
        gone = self._collect()  # what brought it along let go of it, or it was replaced
        self._resolve()  # first: what is read while they stop names none of them
        for entry in gone:
            is_changed = True
            await self._stop(entry, _waiting())
            self._gone.append(entry)
        for entry in _users_first([e for e in self._entries.values() if e.is_started and e.key not in self._wanted]):
            is_changed = True
            await self._stop(entry, _waiting())
        for entry in list(self._entries.values()):
            bound = self._handed(entry)
            if entry.state == PeripheralState.RUNNING and _identity(bound) != _identity(entry.bindings):
                is_changed = True
                await self._deliver(entry, bound)
            elif entry.state in (PeripheralState.WAITING, PeripheralState.FAILED, PeripheralState.DISABLED):
                self._bind_view(entry, bound)
                if entry.stopped_with is not None and _identity(bound) != entry.stopped_with:
                    entry.stopped_with = None
        for entry in self._entries.values():
            if entry.state == PeripheralState.WAITING:
                if (status := self._why_waiting(entry)) is None:
                    self._start(entry)
                else:
                    entry.kind, entry.detail = status.kind, status.detail
        return is_changed or self._snapshot() != before

    def _snapshot(self) -> list[tuple[str, tuple[Endpoint | Peripheral, ...]]]:
        return [(entry.key, tuple(_read(entry.peripheral))) for entry in self._entries.values()]

    def _collect(self) -> list[_Entry]:
        """Every peripheral given, and what the ones that run brought along, in binding order; the ones gone are
        returned."""
        entries: dict[str, _Entry] = {}
        duplicates: list[Peripheral] = []
        gone: list[_Entry] = []

        def walk(peripheral: Peripheral, contributor: Peripheral | None):
            peripheral.contributor = contributor
            key = peripheral.key
            if (seen := entries.get(key)) is not None:
                if seen.peripheral is not peripheral:
                    duplicates.append(peripheral)
                return
            entry = self._entries.get(key)
            if entry is None or entry.peripheral is not peripheral:
                if entry is not None:
                    gone.append(entry)  # replaced by another of the same key
                entry = self._adopt(peripheral)
            entries[key] = entry
            if entry.state == PeripheralState.RUNNING:
                for child in _brought(peripheral):
                    walk(child, peripheral)

        for peripheral in self.given:
            walk(peripheral, None)
        gone += [entry for key, entry in self._entries.items() if key not in entries]
        for duplicate in duplicates:
            if all(duplicate is not known for known in self._duplicates):
                log.error(f"Another peripheral has the key {duplicate.key}: {duplicate.name} is left out")
        self._entries, self._duplicates = entries, duplicates
        return gone

    def _adopt(self, peripheral: Peripheral) -> _Entry:
        key = peripheral.key
        peripheral.output, peripheral.platform = self._shared_output, self.platform
        status = _waiting()
        if key in self._failed:
            status = PeripheralStatus(PeripheralState.FAILED, StatusKind.GAVE_UP, self._failed[key])
        status = self._invalid_settings(peripheral, key) or status
        if self.disabled.is_disabled(key):
            status = PeripheralStatus(PeripheralState.DISABLED)
        return _Entry(peripheral, key, status.state, status.kind, status.detail)

    def _invalid_settings(self, peripheral: Peripheral, key: str) -> PeripheralStatus | None:
        """Load its settings: the failure of one whose kept ones don't validate, which keeps them as they are."""
        try:
            self._load_settings(peripheral, key)
        except ValidationError as error:
            invalid = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
            return PeripheralStatus(PeripheralState.FAILED, StatusKind.INVALID_SETTINGS, f"Invalid settings: {invalid}")
        return None

    def _check_settings(self, entry: _Entry):
        """Read its kept settings again as it is let start: one whose don't validate stays failed until updated."""
        if status := self._invalid_settings(entry.peripheral, entry.key):
            entry.state, entry.kind, entry.detail = status.state, status.kind, status.detail

    def _load_settings(self, peripheral: Peripheral, key: str):
        """The ones kept, else the ones it was given or the defaults, kept from now on; kept ones it no longer has (its
        update dropped them, or made them read-only) go."""
        model, stored = peripheral.Settings, self.state["settings"].get(key)
        if stored is not None:
            kept = model.model_fields.keys() - _read_only(model)
            peripheral.settings = model.model_validate({k: v for k, v in stored.items() if k in kept})
        elif not isinstance(getattr(peripheral, "settings", None), model):
            peripheral.settings = model()
        if _kept(peripheral.settings) != (stored or {}):  # freeze generated defaults
            self._keep_settings(key, peripheral.settings)

    def _keep_settings(self, key: str, settings: BaseModel):
        """Keep ``settings`` from now on; nothing for a peripheral without any."""
        if dumped := _kept(settings):
            self.state["settings"][key] = dumped
        else:
            self.state["settings"].pop(key, None)
        self._save()

    def _resolve(self):
        """The ones that should run: from the ones alive that run anyway, down through what they bind, binding in order
        with single-use endpoints taken; one whose required need finds nothing is left out, and it all binds anew."""
        alive = {key for key, entry in self._entries.items() if entry.state not in _GIVEN_UP}
        provides = {key: _offered(entry.peripheral) for key, entry in self._entries.items()}
        left_out: set[str] = set()
        while True:
            among = alive - left_out
            wanted = {key for key in among if not self._entries[key].peripheral.is_on_demand}
            while True:
                bound, taken = self._bind(wanted, among, provides)
                used = {p.key for b in bound.values() for bindings in b.values() for p, _ in bindings}
                if used <= wanted:
                    break
                wanted |= used
            if not (unbound := wanted - bound.keys()):
                break
            left_out |= unbound
        self._wanted, self._bound = wanted, bound
        self._unbound = _Reasons(self, alive, wanted, provides, taken).all()

    def _bind(
        self, wanted: set[str], among: set[str], provides: Mapping[str, list[Endpoint]]
    ) -> tuple[dict[str, dict[str, list[Binding]]], dict[tuple[str, str, str], str]]:
        """Bind the needs of every wanted one in order to providers ``among``; one whose required need finds nothing
        is left out."""
        bound: dict[str, dict[str, list[Binding]]] = {}
        taken: dict[tuple[str, str, str], str] = {}  # (provider, family, url) of a single-use endpoint -> who took it
        for key in [key for key in self._entries if key in wanted]:
            owner, mine, bindings = self._entries[key].peripheral, [], {}
            for need in owner.needs:
                chosen = [
                    (provider, endpoint)
                    for provider, endpoint in self._candidates(key, need, among, provides)
                    if (provider.key, endpoint.family, endpoint.url) not in taken
                ]
                chosen = chosen if need.is_many else chosen[:1]
                for provider, endpoint in chosen:
                    if not endpoint.is_shared:
                        taken[provider.key, endpoint.family, endpoint.url] = key
                        mine.append((provider.key, endpoint.family, endpoint.url))
                if chosen:
                    bindings[need.family] = chosen
                elif need.is_required:
                    for held in mine:  # what it took goes back to the others
                        del taken[held]
                    break
            else:
                bound[key] = bindings
        return bound, taken

    def _candidates(
        self, owner: str, need: Need, among: set[str], provides: Mapping[str, list[Endpoint]]
    ) -> list[Binding]:
        """What could serve ``need`` of ``owner``, in the order given."""
        return [
            (self._entries[key], endpoint)
            for key in self._entries
            if key in among and key != owner
            for endpoint in provides[key]
            if need.accepts(endpoint)
        ]

    def _why_waiting(self, entry: _Entry) -> PeripheralStatus | None:
        """Why a waiting one doesn't start now; None when it can."""
        if entry.stopped_with is not None:
            return _waiting(StatusKind.STOPPED_ITSELF, "stopped by itself: waits for its needs to change or a restart")
        contributor = self._entries.get(by.key) if (by := entry.peripheral.contributor) else None
        if contributor is not None and contributor.state != PeripheralState.RUNNING:  # it goes with the next pass
            return _waiting(detail=f"comes with {contributor.key}, which stopped")
        if entry.key not in self._wanted:
            required = {need.family for need in entry.peripheral.needs if need.is_required}
            if reasons := [why for family, why in self._unbound.get(entry.key, {}).items() if family in required]:
                return _waiting(StatusKind.NEEDS_UNBOUND, "; ".join(reasons))
            return _waiting(StatusKind.UNUSED, "nothing that runs uses it")
        if entry.retry_at > monotonic():
            return _waiting(StatusKind.RETRYING, entry.failure)
        bound = self._bound[entry.key]
        required = [n.family for n in entry.peripheral.needs if n.is_required and n.family in bound]
        pending = dict.fromkeys(p.key for family in required for p, _ in bound[family] if not self._is_serving(p.key))
        return _waiting(StatusKind.NEEDS_STARTING, f"waiting for {', '.join(pending)}") if pending else None

    def _is_serving(self, key: str) -> bool:
        entry = self._entries[key]
        return entry.state == PeripheralState.RUNNING

    def _handed(self, entry: _Entry) -> dict[str, list[Binding]]:
        """What it is bound to, an optional need only to a provider that runs, or that it was handed before: the start
        doesn't wait for it, and one restarting is a blip."""
        optional = {need.family for need in entry.peripheral.needs if not need.is_required}
        handed: dict[str, list[Binding]] = {}
        for family, bindings in self._bound.get(entry.key, {}).items():
            before = entry.bindings.get(family, [])
            if family in optional:
                bindings = [b for b in bindings if self._is_serving(b[0].key) or b in before]
            if bindings:
                handed[family] = bindings
        return handed

    def _held(self, entry: _Entry) -> dict[str, list[Binding]]:
        """What it was started or last told with; what it would be handed, while it doesn't run."""
        return entry.bindings if entry.is_started else self._handed(entry)

    def _bind_view(self, entry: _Entry, bound: Mapping[str, list[Binding]]):
        entry.peripheral.peripherals = Peripherals(_view(entry.peripheral, bound))

    def _start(self, entry: _Entry):
        entry.state, entry.kind, entry.detail = PeripheralState.STARTING, None, ""
        entry.bindings, entry.told_failure, entry.is_stopping_itself = self._handed(entry), "", False
        entry.platform_before = entry.settings_before = None
        self._bind_view(entry, entry.bindings)
        entry.task = self._spawn(self._run_start(entry))

    async def _run_start(self, entry: _Entry):
        peripheral, failure = entry.peripheral, ""
        timeout = peripheral.start_timeout
        try:
            await _within(timeout, peripheral.start(), f"didn't start within {timeout} s")
        except asyncio.CancelledError as error:
            if _is_cancelled():  # stopped
                raise
            failure = _failure(error)  # raised from inside, e.g. by a task it awaited
        except Exception as error:
            log.error(f"{peripheral.name} failed to start: {error}", exc_info=error)
            failure = _failure(error)
        async with self._locked():
            if entry.task is not asyncio.current_task():  # stopped, though it went on
                return
            entry.task = None
            if failure := entry.told_failure or failure:
                await self._stop(entry, _waiting())  # it releases what it took before it failed
                self._fail(entry, failure)
            elif entry.is_stopping_itself:
                await self._stop_itself(entry)
            else:
                entry.state, entry.running_since = PeripheralState.RUNNING, monotonic()
                await self._tell_missed(entry)
        await self.refresh()

    async def _tell_missed(self, entry: _Entry):
        """Tell one that just started what changed while it started, from what it may have read."""
        platform, settings = entry.platform_before, entry.settings_before
        entry.platform_before = entry.settings_before = None
        if platform is not None and not await self._ask(entry, entry.peripheral.platform_facts_did_change(platform)):
            return  # restarted: it reads the settings anew
        if settings is not None:
            await self._ask(entry, entry.peripheral.settings_did_change(settings))

    async def _stop_itself(self, entry: _Entry):
        stopped_with = _identity(entry.bindings)
        await self._stop(entry, _waiting())
        entry.stopped_with = stopped_with

    # TODO: stop reason as an enum + per-reason restart policies
    def _fail(self, entry: _Entry, reason: str):
        """Count a failure: wait to try again, or give up once the retries are spent."""
        entry.attempts += 1
        delays = RETRY_DELAYS if self._retry_delays is None else self._retry_delays
        if entry.attempts > len(delays):
            entry.state, entry.kind, entry.detail, entry.attempts = (
                PeripheralState.FAILED,
                StatusKind.GAVE_UP,
                reason,
                0,
            )
            self._failed[entry.key] = reason
            return
        delay = delays[entry.attempts - 1]
        entry.state, entry.kind, entry.retry_at = PeripheralState.WAITING, StatusKind.RETRYING, monotonic() + delay
        entry.detail = entry.failure = f"{reason}; trying again in {delay} s"

    async def _stop(self, entry: _Entry, status: PeripheralStatus):
        """Stop it if it runs or starts, and leave it in ``status``. What runs on the very object it provides (a
        required need bound to an endpoint with its ``instance``) stops first: what it registered there goes with it,
        so it starts again only once it runs anew."""
        if entry.state == PeripheralState.DISABLED and status.state != PeripheralState.DISABLED:
            return  # only enabling ends it
        if entry.is_started:
            for user in _users_first([user for user in self._entries.values() if _runs_inside(user, entry)]):
                await self._stop(user, _waiting())
        is_started = entry.is_started
        if task := entry.task:
            entry.task = None
            task.cancel()
            if not (await asyncio.wait({task}, timeout=STOP_TIMEOUT))[0]:
                log.error(f"{entry.key} goes on starting though stopped: left behind")
        if entry.running_since is not None and monotonic() - entry.running_since >= STABLE_RUN:
            entry.attempts = 0
        entry.running_since = None
        if is_started:
            try:
                await _within(STOP_TIMEOUT, entry.peripheral.stop(), f"didn't stop within {STOP_TIMEOUT} s")
            except Exception as error:
                log.error(f"{entry.key} failed to stop cleanly: {error}", exc_info=error)
        entry.state, entry.kind, entry.detail, entry.bindings = status.state, status.kind, status.detail, {}

    async def _deliver(self, entry: _Entry, bound: dict[str, list[Binding]]):
        """Tell one that runs what moved; restart it unless it handles every change."""
        before, entry.bindings = entry.bindings, bound
        self._bind_view(entry, bound)
        was, now = _view(entry.peripheral, before), _view(entry.peripheral, bound)
        for family in sorted(was.keys() | now.keys()):
            if _identity({family: before.get(family, [])}) != _identity({family: bound.get(family, [])}):
                change = PeripheralChange(family, was.get(family), now.get(family))
                if not await self._ask(entry, entry.peripheral.needed_peripheral_did_change(change)):
                    return

    async def _ask(self, entry: _Entry, hook: Awaitable[bool]) -> bool:
        """Whether it handled a change in place; if not it restarts, and a hook that raised counts as a failure."""
        failure = ""
        try:
            if await _within(HOOK_TIMEOUT, hook, f"didn't take the change within {HOOK_TIMEOUT} s"):
                return True
        except asyncio.CancelledError as error:
            if _is_cancelled():
                raise
            log.error(f"{entry.key} was cancelled taking a change", exc_info=error)  # from inside: a call it awaited
            failure = _failure(error)
        except Exception as error:
            log.error(f"{entry.key} failed to take a change: {error}", exc_info=error)
            failure = _failure(error)
        await self._stop(entry, _waiting())
        if failure:
            self._fail(entry, failure)
        return False

    def _revive(self, entry: _Entry):
        """The user's say: its failures in a row, a retry it waits for and its stopping by itself are forgotten."""
        entry.attempts, entry.retry_at, entry.stopped_with = 0, 0.0, None
        self._failed.pop(entry.key, None)

    def _report(self):
        gone, self._gone = self._gone, []
        for entry in gone:
            if self.output and entry.reported is not None:
                with _logged(f"Reporting {entry.key} gone"):
                    self.output.peripheral_did_disappear(entry.peripheral)
        for entry in self._entries.values():
            if entry.reported != (status := entry.status):
                entry.reported = status
                if self.output:
                    with _logged(f"Reporting {entry.key} {status.state}"):
                        self.output.peripheral_did_change_status(entry.peripheral, status)

    def _schedule_retry(self):
        if self._timer:
            self._timer.cancel()
        now = monotonic()
        waiting = [e for e in self._entries.values() if e.state == PeripheralState.WAITING]
        retries = [e.retry_at for e in waiting if e.retry_at > now]
        if retries and self._is_running:
            delay = min(retries) - now
            self._timer = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self.refresh()))

    def _save(self):
        write_json(self.data / "state.json", self.state)

    def _spawn(self, awaitable: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


@dataclass
class _Reasons:
    """Why each need of every peripheral alive isn't bound, from the final binding."""

    runtime: Runtime
    alive: set[str]
    wanted: set[str]
    provides: Mapping[str, list[Endpoint]]
    taken: Mapping[tuple[str, str, str], str]
    reasons: dict[str, dict[str, str]] = field(default_factory=dict)

    def all(self) -> dict[str, dict[str, str]]:
        for key in self.runtime._entries:
            if key in self.alive:
                self.of(key)
        return {key: reasons for key, reasons in self.reasons.items() if reasons}

    def of(self, key: str) -> dict[str, str]:
        if key in self.reasons:
            return self.reasons[key]
        self.reasons[key] = reasons = {}  # also guards against cycles
        bound = self.runtime._bound.get(key, {})
        for need in self.runtime._entries[key].peripheral.needs:
            if need.family not in bound and (why := self.why(key, need)):
                reasons[need.family] = why
        return reasons

    def why(self, owner: str, need: Need) -> str | None:
        """Why ``need`` of ``owner`` can't be bound; None when it could be, had the owner a reason to run."""
        rejected: list[str] = []
        entries = self.runtime._entries
        for provider, endpoint in self.runtime._candidates(owner, need, set(entries), self.provides):
            if (taker := self.taken.get((provider.key, endpoint.family, endpoint.url))) not in (None, owner):
                rejected.append(f"every provider taken by {taker}" + self.hint(taker, need.family))
            elif provider.key not in self.alive:
                rejected.append(f"{provider.key} is {entries[provider.key].state}")
            elif provider.key not in self.wanted and (why := self.blocking(provider)):
                rejected.append(f"{provider.key} can't run ({why})")
            else:
                return None  # free, or on demand and idle only because the owner doesn't run
        wanted = f"{need.family}/{'|'.join(need.dialects) or '*'}"
        return f"need {wanted}: {'; '.join(dict.fromkeys(rejected)) or 'nothing offers it'}"

    def blocking(self, provider: _Entry) -> str:
        """Why the required needs of ``provider`` can't be bound: the optional ones don't keep it from running."""
        required = {need.family for need in provider.peripheral.needs if need.is_required}
        return "; ".join(why for family, why in self.of(provider.key).items() if family in required)

    def hint(self, taker: str, family: str) -> str:
        """What to need instead when what took the endpoint offers the same family itself: the router for raw BLE."""
        endpoint = next((e for e in self.provides.get(taker, []) if e.family == family), None)
        return f' — use Need("{family}", ["{endpoint.dialect}"])' if endpoint else ""


async def _within[T](seconds: float, call: Awaitable[T], late: str) -> T:
    """What ``call`` returns, or TimeoutError(``late``) once it took ``seconds``."""
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            return await call
    except TimeoutError:
        if timeout.expired():
            raise TimeoutError(late) from None
        raise


def _is_read_only(schema: Any) -> bool:
    """Whether a settings field is marked read-only in its JSON schema: its peripheral fills it in, never the user."""
    return isinstance(schema, dict) and schema.get("readOnly") is True


def _read_only(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if _is_read_only(field.json_schema_extra)}


def _kept(settings: BaseModel) -> dict[str, Any]:
    """What is kept of ``settings``: never what their peripheral fills in, shown only while it runs."""
    return settings.model_dump(mode="json", exclude=_read_only(type(settings)))


def _failure(error: BaseException) -> str:
    return str(error) or type(error).__name__


def _is_cancelled() -> bool:
    """Whether the current task is being cancelled, rather than a CancelledError raised from inside what it awaits."""
    return bool((task := asyncio.current_task()) and task.cancelling())


def _read(peripheral: Peripheral) -> list[Endpoint | Peripheral]:
    """What ``peripheral`` provides; one that can't say provides nothing, and the others run on."""
    try:
        return list(peripheral.provides)
    except Exception as error:
        log.error(f"{peripheral.name} failed to say what it provides: {error}", exc_info=error)
        return []


def _offered(peripheral: Peripheral) -> list[Endpoint]:
    return [item for item in _read(peripheral) if isinstance(item, Endpoint)]


def _brought(peripheral: Peripheral) -> list[Peripheral]:
    return [item for item in _read(peripheral) if not isinstance(item, Endpoint)]


@contextlib.contextmanager
def _logged(what: str):
    """A call into the runtime's owner that raises is logged: the runtime goes on."""
    try:
        yield
    except Exception as error:
        log.error(f"{what} failed: {error}", exc_info=error)


def _users_first(entries: list[_Entry]) -> list[_Entry]:
    """``entries`` in the order to stop them: each after what it was started with is bound to it, else last given
    first."""
    left, ordered = entries[::-1], []
    while left:
        entry = next((e for e in left if not any(_is_using(user, e) for user in left)), left[0])
        left.remove(entry)
        ordered.append(entry)
    return ordered


def _is_using(user: _Entry, provider: _Entry) -> bool:
    return any(p is provider for bindings in user.bindings.values() for p, _ in bindings)


def _runs_inside(user: _Entry, provider: _Entry) -> bool:
    """Whether ``user`` runs with a required need bound to the in-process object ``provider`` runs: a zeroconf browser,
    whose restart it can't ride out as it does a server's."""
    required = {need.family for need in user.peripheral.needs if need.is_required}
    return user.is_started and any(
        p is provider and endpoint.instance is not None
        for family, bindings in user.bindings.items()
        if family in required
        for p, endpoint in bindings
    )


def _waiting(kind: StatusKind | None = None, detail: str = "") -> PeripheralStatus:
    return PeripheralStatus(PeripheralState.WAITING, kind, detail)


def _view(peripheral: Peripheral, bound: Mapping[str, list[Binding]]) -> dict[str, Bound]:
    many = {need.family for need in peripheral.needs if need.is_many}
    return {family: [e for _, e in b] if family in many else b[0][1] for family, b in bound.items()}


def _identity(bound: Mapping[str, list[Binding]]) -> _Identity:
    """What tells two bindings apart: the provider, the endpoint and the very instance it carries."""
    return {family: [(p, e, id(e.instance)) for p, e in bindings] for family, bindings in bound.items()}


def _is_fed(needs: Sequence[Need], families: set[str], available: set[str]) -> bool:
    """Whether ``families`` feed one of the required ``needs``, and with what is ``available`` all of them."""
    required = {need.family for need in needs if need.is_required}
    return bool(required & families) and required <= families | available


__all__ = [
    "Runtime",
    "RuntimeOutput",
    "PeripheralState",
    "PeripheralStatus",
    "StatusKind",
    "DisabledStore",
    "MemoryDisabledStore",
    "RETRY_DELAYS",
]
