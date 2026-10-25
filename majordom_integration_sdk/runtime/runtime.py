"""Run peripherals by one rule and bind their needs — what the runtime is given and what those bring along — then keep
it in sync as sticks come and go, providers change, and the user enables, disables or restarts them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from pydantic import ValidationError

from majordom_integration_sdk.controller.platform import Context
from majordom_integration_sdk.peripherals import (
    Bound,
    Endpoint,
    Need,
    Peripheral,
    PeripheralChange,
    Peripherals,
)
from majordom_integration_sdk.runtime.files import read_json, write_json

log = logging.getLogger(__name__)

RETRY_DELAYS = (5, 30, 120)  # seconds before each new start of a peripheral that failed, then it is given up on
STABLE_RUN = 300  # seconds of running that clear the failures in a row
HOOK_TIMEOUT = 30  # seconds a hook may take: it runs while the runtime waits
STOP_TIMEOUT = 15  # a stop stuck on a vanished radio (zigpy awaiting its dead I/O thread) is abandoned
_PASSES = 10  # bound on the passes one change takes to settle: each may change what the next one binds


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
    KEY_TAKEN = "key-taken"  # failed: another peripheral has its key


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
        """`Context` fields it read from what it runs (a border router's Thread network): merge them into the context
        the runtime is given, then tell it (`Runtime.context_did_change`)."""
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
    context_before: Context | None = None  # what the context was when it changed while starting: told once it runs
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
    needs, and is the ``runtime`` of each::

        runtime = Runtime(data, [Pinned(*mine), FixedPeripherals(*soldered), *default_peripherals(), *integrations])
        await runtime.start()

    ``peripherals`` bind in the order given, what each brings along right after it: one given first wins a single-use
    endpoint (the Bluetooth router takes every adapter before the containers) and serves a need first: `Pinned`,
    then the hardware, the defaults and the integrations. ``context`` gives the Hub-wide facts, which peripherals read
    and containers render; ``output`` hears every change of status, what is learned and what is gone; ``disabled``
    keeps what the user disabled (in memory by default). Its own state, the settings and the user's reservations, goes
    to ``data``; every peripheral gets a folder there, ``peripherals/<key>``.

    A peripheral that isn't disabled or failed runs while something that runs binds it if it ``is_on_demand``, while
    what brought it along runs, and once every required need is bound to a provider that runs. Only a waiting one
    starts by itself: a stick plugged in again never revives a disabled or failed one. A start that raises or takes too
    long, a crash it tells (``peripheral_did_fail``) and a hook that raises are tried again after each of
    `RETRY_DELAYS`, then it is failed; one that stops by itself (``peripheral_did_stop``) waits for its needs to
    change. What it brought along goes when it stops.
    """

    def __init__(
        self,
        data: Path,
        peripherals: Sequence[Peripheral] = (),
        *,
        context: Callable[[], Context] = Context,
        output: RuntimeOutput | None = None,
        disabled: DisabledStore | None = None,
    ):
        self.data, self.given, self._context, self.output = data, list(peripherals), context, output
        self.disabled = disabled or MemoryDisabledStore()
        self.state: dict[str, Any] = {"settings": {}, "reserved": {}}
        saved = read_json(data / "state.json")
        self.state |= {key: saved[key] for key in saved.keys() & self.state.keys()}  # older keys start fresh
        self._entries: dict[str, _Entry] = {}  # in binding order
        self._clashes: list[_Entry] = []  # failed: another took their key
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
        """Collect, bind and run everything anew now."""
        if not self._is_running:
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
        """Every peripheral it runs, in binding order: what it was given and what those brought along; then the ones
        that failed as another took their key."""
        return [entry.peripheral for entry in [*self._entries.values(), *self._clashes]]

    def peripheral(self, key: str) -> Peripheral:
        return self._entries[key].peripheral

    def status(self, peripheral: Peripheral) -> PeripheralStatus:
        return self._entry(peripheral).status

    def bound(self, peripheral: Peripheral) -> dict[str, Bound]:
        """What each need is bound to, by family: an `Endpoint`, a list of them for an ``is_many`` need."""
        return _view(peripheral, self._bound.get(self._entry(peripheral).key, {}))

    def providers(self, peripheral: Peripheral) -> dict[str, str]:
        """Who serves each bound need, by family; several, comma-separated, for an ``is_many`` need."""
        bound = self._bound.get(self._entry(peripheral).key, {})
        return {family: ", ".join(p.key for p, _ in bindings) for family, bindings in bound.items()}

    def unbound(self, peripheral: Peripheral) -> dict[str, str]:
        """Why each need isn't bound, by family: for the user, with what to do when there is something, e.g.
        'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'."""
        return dict(self._unbound.get(self._entry(peripheral).key, {}))

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
        """Run ``peripheral`` too, as if given last: an integration installed while the hub runs."""
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

        await self._act(apply)

    async def disable(self, peripheral: Peripheral):
        """Stop it, and what it brought along, until enabled again; kept across restarts."""
        entry = self._entry(peripheral)
        self.disabled.set_disabled(entry.key, True)
        await self._act(lambda: self._stop(entry, PeripheralStatus(PeripheralState.DISABLED)))

    async def restart(self, peripheral: Peripheral):
        """Stop it if it runs and start it again, a failed one or one that stopped by itself too: its retries start
        over. Called from inside a peripheral's own start, stop or hook, it happens once that call returns."""
        entry = self._entry(peripheral)
        if entry.state == PeripheralState.DISABLED:
            raise ValueError(f"{entry.key} is disabled: enable it")

        async def apply():
            await self._stop(entry, _waiting())
            self._revive(entry)

        await self._act(apply)

    async def update_settings(self, peripheral: Peripheral, values: Mapping[str, Any]) -> Any:
        """Validate and keep new values of its ``Settings``; one that runs hears it (`settings_did_change`), a failed
        one or one that stopped by itself tries again."""
        entry = self._entry(peripheral)
        if (model := peripheral.Settings) is None:
            raise KeyError(f"{entry.key} has no settings")
        before = peripheral.settings
        peripheral.settings = model.model_validate(self.state["settings"].get(entry.key, {}) | dict(values))
        self.state["settings"][entry.key] = peripheral.settings.model_dump(mode="json")
        self._save()

        async def apply():
            if entry.state in (PeripheralState.FAILED, PeripheralState.WAITING):
                entry.state = PeripheralState.WAITING
                self._revive(entry)
            elif entry.state == PeripheralState.RUNNING:
                await self._ask(entry, peripheral.settings_did_change(before))
            elif entry.state == PeripheralState.STARTING:  # it may have read the ones before: start it anew
                await self._stop(entry, _waiting())

        await self._act(apply)
        return peripheral.settings

    async def context_did_change(self, before: Context):
        """The Hub-wide facts changed: every peripheral that runs hears it (`Peripheral.context_did_change`); one
        still starting may have read the ones before, so it hears it once it runs."""

        async def apply():
            for entry in list(self._entries.values()):
                if entry.state == PeripheralState.RUNNING:
                    await self._ask(entry, entry.peripheral.context_did_change(before))
                elif entry.state == PeripheralState.STARTING and entry.context_before is None:
                    entry.context_before = before

        await self._act(apply)

    async def reserve(self, owner: str, family: str, url: str | None):
        """Keep the ``family`` endpoint at ``url`` for ``owner`` alone, bound to it before anything else can bind it,
        ``is_many`` needs included: one Bluetooth adapter, ``ble/bluez hci1``, for matterjs-server to use directly.
        None gives it back."""
        if url is None:
            self.state["reserved"].get(owner, {}).pop(family, None)
        else:
            self.state["reserved"].setdefault(owner, {})[family] = url
        self._save()
        await self._settle()

    # PeripheralOutput

    @property
    def context(self) -> Context:
        return self._context()

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        await self._settle()

    async def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        if self.output and self._find(peripheral) is not None:
            with _logged(f"Reporting what {peripheral.name} learned"):
                self.output.peripheral_did_learn(peripheral, facts)

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
        return next((e for e in [*self._entries.values(), *self._clashes] if e.peripheral is peripheral), None)

    async def _act(self, change: Callable[[], Awaitable[Any]]):
        """Make a change and settle; from inside a call the runtime waits on, once that returns."""
        if self._is_driving():
            self._spawn(self._act_unlocked(change))
            return
        await self._act_unlocked(change)

    async def _act_unlocked(self, change: Callable[[], Awaitable[Any]]):
        async with self._locked():
            await change()
        await self.refresh()

    async def _settle(self):
        """Bind and run anew: now, or from inside a call the runtime waits on, once that returns."""
        if self._is_driving():
            self._spawn(self.refresh())
        else:
            await self.refresh()

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
        for entry in self._collect():  # gone: what brought it along let go of it, or it was replaced
            is_changed = True
            await self._stop(entry, _waiting())
            self._gone.append(entry)
        self._resolve()
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
        clashes: list[_Entry] = []
        gone: list[_Entry] = []
        known = {id(entry.peripheral): entry for entry in [*self._entries.values(), *self._clashes]}

        def walk(peripheral: Peripheral, contributor: Peripheral | None):
            peripheral.contributor = contributor
            key = peripheral.key
            if (seen := entries.get(key)) is not None:
                if seen.peripheral is not peripheral:
                    clashes.append(known.get(id(peripheral)) or self._clash(peripheral))
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
        self._gone += [clash for clash in self._clashes if all(clash is not c for c in clashes)]
        self._entries, self._clashes = entries, clashes
        return gone

    def _adopt(self, peripheral: Peripheral) -> _Entry:
        key = peripheral.key
        peripheral.runtime, peripheral.data = self, self.data / "peripherals" / key
        status = _waiting()
        if key in self._failed:
            status = PeripheralStatus(PeripheralState.FAILED, StatusKind.GAVE_UP, self._failed[key])
        try:
            self._load_settings(peripheral, key)
        except ValidationError as error:
            invalid = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
            status = PeripheralStatus(
                PeripheralState.FAILED, StatusKind.INVALID_SETTINGS, f"Invalid settings: {invalid}"
            )
        if self.disabled.is_disabled(key):
            status = PeripheralStatus(PeripheralState.DISABLED)
        return _Entry(peripheral, key, status.state, status.kind, status.detail)

    def _clash(self, peripheral: Peripheral) -> _Entry:
        log.error(f"Two peripherals have the key {peripheral.key}: the second fails")
        detail = f"another peripheral has the key {peripheral.key}"
        return _Entry(peripheral, peripheral.key, PeripheralState.FAILED, StatusKind.KEY_TAKEN, detail)

    def _load_settings(self, peripheral: Peripheral, key: str):
        """The ones kept, else the ones it was given or the defaults, kept from now on."""
        if (model := peripheral.Settings) is None:
            return
        stored = self.state["settings"].get(key)
        if stored is not None:
            peripheral.settings = model.model_validate(stored)
        elif not isinstance(peripheral.settings, model):
            peripheral.settings = model()
        if (dumped := peripheral.settings.model_dump(mode="json")) != stored:  # freeze generated defaults (e.g. keys)
            self.state["settings"][key] = dumped
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
                    if self._is_free(provider, endpoint, key, taken)
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
        """What could serve ``need`` of ``owner``, best first: reserved for it, then in the order given."""
        candidates = [
            (self._entries[key], endpoint)
            for key in self._entries
            if key in among and key != owner
            for endpoint in provides[key]
            if need.accepts(endpoint)
        ]
        reserved = self.state["reserved"].get(owner, {})
        return sorted(candidates, key=lambda c: reserved.get(c[1].family) != c[1].url)

    def _is_free(
        self, provider: _Entry, endpoint: Endpoint, owner: str, taken: Mapping[tuple[str, str, str], str]
    ) -> bool:
        """Whether ``owner`` may bind ``endpoint``: no one took it, and it isn't reserved for another."""
        reserved = self.state["reserved"].items()
        is_kept = any(kept.get(endpoint.family) == endpoint.url and key != owner for key, kept in reserved)
        return not is_kept and (provider.key, endpoint.family, endpoint.url) not in taken

    def _why_waiting(self, entry: _Entry) -> PeripheralStatus | None:
        """Why a waiting one doesn't start now; None when it can."""
        if entry.stopped_with is not None:
            return _waiting(StatusKind.STOPPED_ITSELF, "stopped by itself: waits for its needs to change or a restart")
        contributor = self._entries.get(by.key) if (by := entry.peripheral.contributor) else None
        if contributor is not None and contributor.state != PeripheralState.RUNNING:  # it goes with the next pass
            return _waiting(StatusKind.UNUSED, f"comes with {contributor.key}, which stopped")
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

    def _bind_view(self, entry: _Entry, bound: Mapping[str, list[Binding]]):
        entry.peripheral.peripherals = Peripherals(_view(entry.peripheral, bound))

    def _start(self, entry: _Entry):
        entry.state, entry.kind, entry.detail = PeripheralState.STARTING, None, ""
        entry.bindings, entry.told_failure, entry.is_stopping_itself = self._handed(entry), "", False
        entry.context_before = None
        self._bind_view(entry, entry.bindings)
        entry.task = self._spawn(self._run_start(entry))

    async def _run_start(self, entry: _Entry):
        peripheral, failure = entry.peripheral, ""
        timeout = peripheral.start_timeout
        try:
            await _within(timeout, peripheral.start(), f"didn't start within {timeout} s")
        except asyncio.CancelledError as error:
            if (task := asyncio.current_task()) and task.cancelling():  # stopped
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
                if (before := entry.context_before) is not None:
                    entry.context_before = None
                    await self._ask(entry, peripheral.context_did_change(before))
        await self.refresh()

    async def _stop_itself(self, entry: _Entry):
        stopped_with = _identity(entry.bindings)
        await self._stop(entry, _waiting())
        entry.stopped_with = stopped_with

    # TODO: stop reason as an enum + per-reason restart policies
    def _fail(self, entry: _Entry, reason: str):
        """Count a failure: wait to try again, or give up once the retries are spent."""
        entry.attempts += 1
        if entry.attempts > len(RETRY_DELAYS):
            entry.state, entry.kind, entry.detail, entry.attempts = (
                PeripheralState.FAILED,
                StatusKind.GAVE_UP,
                reason,
                0,
            )
            self._failed[entry.key] = reason
            return
        delay = RETRY_DELAYS[entry.attempts - 1]
        entry.state, entry.kind, entry.retry_at = PeripheralState.WAITING, StatusKind.RETRYING, monotonic() + delay
        entry.detail = entry.failure = f"{reason}; trying again in {delay} s"

    async def _stop(self, entry: _Entry, status: PeripheralStatus):
        """Stop it if it runs or starts, and leave it in ``status``."""
        if entry.state == PeripheralState.DISABLED and status.state != PeripheralState.DISABLED:
            return  # only enabling ends it
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
        for entry in [*self._entries.values(), *self._clashes]:
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
        entries, reserved = self.runtime._entries, self.runtime.state["reserved"]
        for provider, endpoint in self.runtime._candidates(owner, need, set(entries), self.provides):
            keeper = next((n for n, kept in reserved.items() if kept.get(need.family) == endpoint.url), owner)
            if keeper != owner:
                rejected.append(f"{endpoint.url or endpoint.dialect} is reserved for {keeper}")
            elif (taker := self.taken.get((provider.key, endpoint.family, endpoint.url))) not in (None, owner):
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


def _failure(error: BaseException) -> str:
    return str(error) or type(error).__name__


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
