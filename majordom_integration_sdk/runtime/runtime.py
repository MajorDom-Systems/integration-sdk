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

from majordom_integration_sdk.controller.abstract_controller import AbstractController
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
START_TIMEOUT = 600  # seconds a start may take to be ready, pulling a container's image included, else it failed
HOOK_TIMEOUT = 30  # seconds a hook may take: it runs while the runtime waits
STOP_TIMEOUT = 15  # a stop stuck on a vanished radio (zigpy awaiting its dead I/O thread) is abandoned
_PASSES = 10  # bound on the passes one change takes to settle: each may change what the next one binds


class PeripheralState(StrEnum):
    DISABLED = "disabled"  # by the user, persisted: until enabled
    WAITING = "waiting"  # for a need, a provider, what brought it along, something to use it, or a retry
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"  # given up on: until restarted or its settings change


@dataclass(frozen=True)
class PeripheralStatus:
    state: PeripheralState
    reason: str = ""  # why it waits or failed, for the user


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
    """Where the user's disabling of a peripheral is kept, by its name here (`Runtime.name`): the Hub's enable/disable
    store."""

    def is_disabled(self, name: str) -> bool: ...
    def set_disabled(self, name: str, is_disabled: bool): ...


class MemoryDisabledStore:
    """Disabled until the process ends: library mode, the dev runner."""

    def __init__(self, disabled: Iterable[str] = ()):
        self.disabled = set(disabled)

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    def set_disabled(self, name: str, is_disabled: bool):
        (self.disabled.add if is_disabled else self.disabled.discard)(name)


_GIVEN_UP = (PeripheralState.DISABLED, PeripheralState.FAILED)

type Binding = tuple[_Entry, Endpoint]  # who provides it, and what
type _Key = dict[str, list[tuple[_Entry, Endpoint, int]]]  # what tells bindings apart


@dataclass(eq=False)
class _Entry:
    peripheral: Peripheral
    name: str  # what it is known by here
    contributor: str | None  # the name of what brought it along
    state: PeripheralState
    reason: str = ""
    attempts: int = 0  # failures in a row
    running_since: float | None = None  # monotonic
    retry_at: float = 0.0  # monotonic: a waiting one starts no earlier
    failure: str = ""  # why, and when it is tried again
    bindings: dict[str, list[Binding]] = field(default_factory=dict)  # what it was started or last told with
    stopped_with: _Key | None = None  # what it was bound to when it stopped by itself: it waits for a change
    told_failure: str = ""  # while starting
    is_stopping_itself: bool = False  # told while starting
    task: asyncio.Task | None = None  # its start
    reported: PeripheralStatus | None = None

    @property
    def status(self) -> PeripheralStatus:
        return PeripheralStatus(self.state, self.reason)

    @property
    def is_started(self) -> bool:
        return self.state in (PeripheralState.STARTING, PeripheralState.RUNNING)


class Runtime:
    """Runs every peripheral it is given, and what those bring along, by one rule (see `Peripheral`); binds their
    needs, and is the `PeripheralOutput` of each::

        runtime = Runtime(data, peripherals=[*default_peripherals(), HueController(platform)])
        await runtime.start()

    ``peripherals`` are bound in this order, what each brings along right after it: one listed first wins a single-use
    endpoint (the Bluetooth router takes every adapter before the containers) and is preferred for a need (list
    `Pinned` first). ``context`` gives the Hub-wide facts, which peripherals read and containers render; ``output``
    hears every change of status, what is learned and what is gone; ``disabled`` keeps what the user disabled (in
    memory by default). Its own state — the settings, the user's pins and reservations — goes to ``data``, and every
    peripheral gets a folder there, ``peripherals/<name>``.

    A peripheral that isn't disabled or failed runs while what brought it along does, while something that runs binds
    it if it ``is_on_demand``, and once every required need is bound to a provider that runs: its start returned. Only a
    waiting one starts by itself: a stick plugged in again never revives a disabled or failed one. A start that raises
    is tried again after each of `RETRY_DELAYS`, then the peripheral is failed; a hook that raises restarts it the same
    way. Leaving the running state releases what it brought along: what it holds stops with it.
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
        self.state: dict[str, Any] = {"settings": {}, "pins": {}, "reserved": {}}
        saved = read_json(data / "state.json")
        self.state |= {key: saved[key] for key in saved.keys() & self.state.keys()}  # older keys start fresh
        self._entries: dict[str, _Entry] = {}  # in binding order
        self._clashes: list[_Entry] = []  # failed: another took their name
        self._gone: list[_Entry] = []  # to report
        self._failed: dict[str, str] = {}  # name -> why, for the session: one brought along anew stays failed
        self._wanted: set[str] = set()  # the ones that should run
        self._bound: dict[str, dict[str, list[Binding]]] = {}
        self._unbound: dict[str, dict[str, str]] = {}  # name -> family -> why its need isn't bound
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
        that failed as another took their name."""
        return [entry.peripheral for entry in [*self._entries.values(), *self._clashes]]

    def peripheral(self, name: str) -> Peripheral:
        return self._entries[name].peripheral

    def name(self, peripheral: Peripheral) -> str:
        """What it is known, disabled and persisted by here: its ``name``, a controller's slug, and for one brought
        along the name of what brought it, then its own: ``usb/<stick>``, ``esphome/<node>``."""
        return self._entry(peripheral).name

    def status(self, peripheral: Peripheral) -> PeripheralStatus:
        return self._entry(peripheral).status

    def bound(self, peripheral: Peripheral) -> dict[str, Bound]:
        """What each need is bound to, by family: an `Endpoint`, a list of them for an ``is_many`` need."""
        return _view(peripheral, self._bound.get(self.name(peripheral), {}))

    def providers(self, peripheral: Peripheral) -> dict[str, str]:
        """Who serves each bound need, by family; several, comma-separated, for an ``is_many`` need."""
        bound = self._bound.get(self.name(peripheral), {})
        return {family: ", ".join(p.name for p, _ in bindings) for family, bindings in bound.items()}

    def unbound(self, peripheral: Peripheral) -> dict[str, str]:
        """Why each need isn't bound, by family: for the user, with what to do when there is something, e.g.
        'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'."""
        return dict(self._unbound.get(self.name(peripheral), {}))

    def users(self, peripheral: Peripheral) -> list[str]:
        """Every peripheral that runs on it, directly or through others: a radio's integrations and their containers."""
        users: list[str] = []
        providers = {self.name(peripheral)}
        while True:
            more = [
                name
                for name, bound in self._bound.items()
                if name not in users and any(p.name in providers for b in bound.values() for p, _ in b)
            ]
            if not more:
                return users
            users += more
            providers.update(more)

    def uses(self, peripheral: Peripheral) -> list[str]:
        """Every peripheral it runs on, directly or through others: an integration's containers and radios."""
        used: list[str] = []
        owners = [self.name(peripheral)]
        while owners:
            bound = self._bound.get(owners.pop(0), {})
            more = [p.name for b in bound.values() for p, _ in b if p.name not in used]
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
                name
                for name, entry in self._entries.items()
                if entry.peripheral.is_on_demand and _is_fed(entry.peripheral.needs, families, available)
            }
            reached = families.union(
                *({e.family for e in _offered(self._entries[name].peripheral)} for name in on_the_way)
            )
        users = [
            entry.peripheral
            for name, entry in self._entries.items()
            if not entry.peripheral.is_on_demand or (name in self._wanted and name not in on_the_way)
        ]
        return sorted(families & {need.family for peripheral in users for need in peripheral.needs})

    # What the Hub (or you) changes

    async def enable(self, peripheral: Peripheral):
        entry = self._entry(peripheral)
        self.disabled.set_disabled(entry.name, False)

        async def apply():
            if entry.state == PeripheralState.DISABLED:
                entry.state = PeripheralState.WAITING
                self._revive(entry)

        await self._act(apply)

    async def disable(self, peripheral: Peripheral):
        """Stop it, and what it brought along, until enabled again; kept across restarts."""
        entry = self._entry(peripheral)
        self.disabled.set_disabled(entry.name, True)
        await self._act(lambda: self._stop(entry, PeripheralStatus(PeripheralState.DISABLED)))

    async def restart(self, peripheral: Peripheral):
        """Stop it if it runs and start it again, a failed one or one that stopped by itself too: its retries start
        over. Called from inside a peripheral's own start, stop or hook, it happens once that call returns."""
        entry = self._entry(peripheral)
        if entry.state == PeripheralState.DISABLED:
            raise ValueError(f"{entry.name} is disabled: enable it")

        async def apply():
            await self._stop(entry, _waiting())
            self._revive(entry)

        await self._act(apply)

    async def update_settings(self, peripheral: Peripheral, values: Mapping[str, Any]) -> Any:
        """Validate and keep new values of its ``Settings``; one that runs hears it (`settings_did_change`), a failed
        one or one that stopped by itself tries again."""
        entry = self._entry(peripheral)
        if (model := peripheral.Settings) is None:
            raise KeyError(f"{entry.name} has no settings")
        before = peripheral.settings
        peripheral.settings = model.model_validate(self.state["settings"].get(entry.name, {}) | dict(values))
        self.state["settings"][entry.name] = peripheral.settings.model_dump(mode="json")
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
        still starting may have read the ones before, so it starts anew."""

        async def apply():
            for entry in list(self._entries.values()):
                if entry.state == PeripheralState.RUNNING:
                    await self._ask(entry, entry.peripheral.context_did_change(before))
                elif entry.state == PeripheralState.STARTING:
                    await self._stop(entry, _waiting())

        await self._act(apply)

    async def pin(self, family: str, provider: str | None):
        """Prefer ``provider`` (a peripheral's name) for every need of ``family``; None goes back to the order given."""
        if provider is None:
            self.state["pins"].pop(family, None)
        else:
            self.state["pins"][family] = provider
        self._save()
        await self._settle()

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
        for entry in _users_first([e for e in self._entries.values() if e.is_started and e.name not in self._wanted]):
            is_changed = True
            await self._stop(entry, _waiting())
        for entry in list(self._entries.values()):
            bound = self._bound.get(entry.name, {})
            if entry.state == PeripheralState.RUNNING and _key(bound) != _key(entry.bindings):
                is_changed = True
                await self._deliver(entry, bound)
            elif entry.state in (PeripheralState.WAITING, PeripheralState.FAILED, PeripheralState.DISABLED):
                self._bind_view(entry, bound)
                if entry.stopped_with is not None and _key(bound) != entry.stopped_with:
                    entry.stopped_with = None
        for entry in self._entries.values():
            if entry.state == PeripheralState.WAITING:
                entry.reason = self._why_waiting(entry)
                if not entry.reason:
                    self._start(entry)
        return is_changed or self._snapshot() != before

    def _snapshot(self) -> list[tuple[str, tuple[Endpoint | Peripheral, ...]]]:
        return [(entry.name, tuple(_read(entry.peripheral))) for entry in self._entries.values()]

    def _collect(self) -> list[_Entry]:
        """Every peripheral given, and what the ones that run brought along, in binding order; the ones gone are
        returned."""
        entries: dict[str, _Entry] = {}
        clashes: list[_Entry] = []
        gone: list[_Entry] = []
        known = {id(entry.peripheral): entry for entry in [*self._entries.values(), *self._clashes]}

        def walk(peripheral: Peripheral, contributor: str | None):
            own = peripheral.slug() if isinstance(peripheral, AbstractController) else peripheral.name
            name = f"{contributor}/{own}" if contributor else own
            if (seen := entries.get(name)) is not None:
                if seen.peripheral is not peripheral:
                    clashes.append(known.get(id(peripheral)) or self._clash(peripheral, name, contributor))
                return
            entry = self._entries.get(name)
            if entry is None or entry.peripheral is not peripheral:
                if entry is not None:
                    gone.append(entry)  # replaced by another of the same name
                entry = self._adopt(peripheral, name, contributor)
            entries[name] = entry
            if entry.state == PeripheralState.RUNNING:
                for child in _brought(peripheral):
                    walk(child, name)

        for peripheral in self.given:
            walk(peripheral, None)
        gone += [entry for name, entry in self._entries.items() if name not in entries]
        self._gone += [clash for clash in self._clashes if all(clash is not c for c in clashes)]
        self._entries, self._clashes = entries, clashes
        return gone

    def _adopt(self, peripheral: Peripheral, name: str, contributor: str | None) -> _Entry:
        peripheral.runtime, peripheral.data = self, self.data / "peripherals" / name
        failure = self._failed.get(name, "")
        try:
            self._load_settings(peripheral, name)
        except ValidationError as error:
            failure = "Invalid settings: " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()
            )
        if self.disabled.is_disabled(name):
            return _Entry(peripheral, name, contributor, PeripheralState.DISABLED)
        state = PeripheralState.FAILED if failure else PeripheralState.WAITING
        return _Entry(peripheral, name, contributor, state, failure)

    def _clash(self, peripheral: Peripheral, name: str, contributor: str | None) -> _Entry:
        log.error(f"Two peripherals are named {name}: the second fails")
        failure = f"another peripheral is named {name}"
        return _Entry(peripheral, name, contributor, PeripheralState.FAILED, failure)

    def _load_settings(self, peripheral: Peripheral, name: str):
        if (model := peripheral.Settings) is None:
            return
        stored = self.state["settings"].get(name, {})
        peripheral.settings = model.model_validate(stored)
        if (dumped := peripheral.settings.model_dump(mode="json")) != stored:  # freeze generated defaults (e.g. keys)
            self.state["settings"][name] = dumped
            self._save()

    def _resolve(self):
        """The ones that should run: from the ones alive that run anyway, down through what they bind, binding in order
        with single-use endpoints taken; one whose required need finds nothing is left out, and it all binds anew."""
        alive = {name for name, entry in self._entries.items() if entry.state not in _GIVEN_UP}
        provides = {name: _offered(entry.peripheral) for name, entry in self._entries.items()}
        left_out: set[str] = set()
        while True:
            among = alive - left_out
            wanted = {name for name in among if not self._entries[name].peripheral.is_on_demand}
            while True:
                bound, taken = self._bind(wanted, among, provides)
                used = {p.name for b in bound.values() for bindings in b.values() for p, _ in bindings}
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
        for name in [name for name in self._entries if name in wanted]:
            owner, mine, bindings = self._entries[name].peripheral, [], {}
            for need in owner.needs:
                chosen = [
                    (provider, endpoint)
                    for provider, endpoint in self._candidates(name, need, among, provides)
                    if self._is_free(provider, endpoint, name, taken)
                ]
                chosen = chosen if need.is_many else chosen[:1]
                for provider, endpoint in chosen:
                    if not endpoint.is_shared:
                        taken[provider.name, endpoint.family, endpoint.url] = name
                        mine.append((provider.name, endpoint.family, endpoint.url))
                if chosen:
                    bindings[need.family] = chosen
                elif need.is_required:
                    for key in mine:  # what it took goes back to the others
                        del taken[key]
                    break
            else:
                bound[name] = bindings
        return bound, taken

    def _candidates(
        self, owner: str, need: Need, among: set[str], provides: Mapping[str, list[Endpoint]]
    ) -> list[Binding]:
        """What could serve ``need`` of ``owner``, best first: reserved for it, then the user's pin, then in order."""
        candidates = [
            (self._entries[name], endpoint)
            for name in self._entries
            if name in among and name != owner
            for endpoint in provides[name]
            if need.accepts(endpoint)
        ]
        pin = self.state["pins"].get(need.family)
        reserved = self.state["reserved"].get(owner, {})
        return sorted(candidates, key=lambda c: (reserved.get(c[1].family) != c[1].url, c[0].name != pin))

    def _is_free(
        self, provider: _Entry, endpoint: Endpoint, owner: str, taken: Mapping[tuple[str, str, str], str]
    ) -> bool:
        """Whether ``owner`` may bind ``endpoint``: no one took it, and it isn't reserved for another."""
        reserved = self.state["reserved"].items()
        is_kept = any(kept.get(endpoint.family) == endpoint.url and name != owner for name, kept in reserved)
        return not is_kept and (provider.name, endpoint.family, endpoint.url) not in taken

    def _why_waiting(self, entry: _Entry) -> str:
        """Why a waiting one doesn't start now; empty when it can."""
        if entry.stopped_with is not None:
            return "stopped by itself: waits for its needs to change or a restart"
        contributor = self._entries.get(entry.contributor or "")
        if contributor is not None and contributor.state != PeripheralState.RUNNING:  # it goes with the next pass
            return f"comes with {contributor.name}, which stopped"
        if entry.name not in self._wanted:
            required = {need.family for need in entry.peripheral.needs if need.is_required}
            reasons = [why for family, why in self._unbound.get(entry.name, {}).items() if family in required]
            return "; ".join(reasons) or "nothing that runs uses it"
        if entry.retry_at > monotonic():
            return entry.failure
        bound = self._bound[entry.name]
        required = [n.family for n in entry.peripheral.needs if n.is_required and n.family in bound]
        pending = dict.fromkeys(p.name for family in required for p, _ in bound[family] if not self._is_serving(p.name))
        return f"waiting for {', '.join(pending)}" if pending else ""

    def _is_serving(self, name: str) -> bool:
        entry = self._entries[name]
        return entry.state == PeripheralState.RUNNING

    def _bind_view(self, entry: _Entry, bound: Mapping[str, list[Binding]]):
        entry.peripheral.peripherals = Peripherals(_view(entry.peripheral, bound))

    def _start(self, entry: _Entry):
        entry.state, entry.reason = PeripheralState.STARTING, ""
        entry.bindings, entry.told_failure, entry.is_stopping_itself = self._bound[entry.name], "", False
        self._bind_view(entry, entry.bindings)
        entry.task = self._spawn(self._run_start(entry))

    async def _run_start(self, entry: _Entry):
        peripheral, failure = entry.peripheral, ""
        try:
            await _within(START_TIMEOUT, peripheral.start(), f"didn't start within {START_TIMEOUT} s")
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
        await self.refresh()

    async def _stop_itself(self, entry: _Entry):
        stopped_with = _key(entry.bindings)
        await self._stop(entry, _waiting())
        entry.stopped_with = stopped_with

    # TODO: stop reason as an enum + per-reason restart policies
    def _fail(self, entry: _Entry, reason: str):
        """Count a failure: wait to try again, or give up once the retries are spent."""
        entry.attempts += 1
        if entry.attempts > len(RETRY_DELAYS):
            entry.state, entry.reason, entry.attempts = PeripheralState.FAILED, reason, 0
            self._failed[entry.name] = reason
            return
        delay = RETRY_DELAYS[entry.attempts - 1]
        entry.state, entry.retry_at = PeripheralState.WAITING, monotonic() + delay
        entry.reason = entry.failure = f"{reason}; trying again in {delay} s"

    async def _stop(self, entry: _Entry, status: PeripheralStatus):
        """Stop it if it runs or starts, and leave it in ``status``."""
        if entry.state == PeripheralState.DISABLED and status.state != PeripheralState.DISABLED:
            return  # only enabling ends it
        is_started = entry.is_started
        if task := entry.task:
            entry.task = None
            task.cancel()
            if not (await asyncio.wait({task}, timeout=STOP_TIMEOUT))[0]:
                log.error(f"{entry.name} goes on starting though stopped: left behind")
        if entry.running_since is not None and monotonic() - entry.running_since >= STABLE_RUN:
            entry.attempts = 0
        entry.running_since = None
        if is_started:
            try:
                await _within(STOP_TIMEOUT, entry.peripheral.stop(), f"didn't stop within {STOP_TIMEOUT} s")
            except Exception as error:
                log.error(f"{entry.name} failed to stop cleanly: {error}", exc_info=error)
        entry.state, entry.reason, entry.bindings = status.state, status.reason, {}

    async def _deliver(self, entry: _Entry, bound: dict[str, list[Binding]]):
        """Tell one that runs what moved; restart it unless it handles every change."""
        before, entry.bindings = entry.bindings, bound
        self._bind_view(entry, bound)
        was, now = _view(entry.peripheral, before), _view(entry.peripheral, bound)
        for family in sorted(was.keys() | now.keys()):
            if _key({family: before.get(family, [])}) != _key({family: bound.get(family, [])}):
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
            log.error(f"{entry.name} failed to take a change: {error}", exc_info=error)
            failure = _failure(error)
        await self._stop(entry, _waiting())
        if failure:
            self._fail(entry, failure)
        return False

    def _revive(self, entry: _Entry):
        """The user's say: its failures in a row, a retry it waits for and its stopping by itself are forgotten."""
        entry.attempts, entry.retry_at, entry.stopped_with = 0, 0.0, None
        self._failed.pop(entry.name, None)

    def _report(self):
        gone, self._gone = self._gone, []
        for entry in gone:
            if self.output and entry.reported is not None:
                with _logged(f"Reporting {entry.name} gone"):
                    self.output.peripheral_did_disappear(entry.peripheral)
        for entry in [*self._entries.values(), *self._clashes]:
            if entry.reported != (status := entry.status):
                entry.reported = status
                if self.output:
                    with _logged(f"Reporting {entry.name} {status.state}"):
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
        for name in self.runtime._entries:
            if name in self.alive:
                self.of(name)
        return {name: reasons for name, reasons in self.reasons.items() if reasons}

    def of(self, name: str) -> dict[str, str]:
        if name in self.reasons:
            return self.reasons[name]
        self.reasons[name] = reasons = {}  # also guards against cycles
        bound = self.runtime._bound.get(name, {})
        for need in self.runtime._entries[name].peripheral.needs:
            if need.family not in bound and (why := self.why(name, need)):
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
            elif (taker := self.taken.get((provider.name, endpoint.family, endpoint.url))) not in (None, owner):
                rejected.append(f"every provider taken by {taker}" + self.hint(taker, need.family))
            elif provider.name not in self.alive:
                rejected.append(f"{provider.name} is {entries[provider.name].state}")
            elif provider.name not in self.wanted and (why := self.blocking(provider)):
                rejected.append(f"{provider.name} can't run ({why})")
            else:
                return None  # free, or on demand and idle only because the owner doesn't run
        wanted = f"{need.family}/{'|'.join(need.dialects) or '*'}"
        return f"need {wanted}: {'; '.join(dict.fromkeys(rejected)) or 'nothing offers it'}"

    def blocking(self, provider: _Entry) -> str:
        """Why the required needs of ``provider`` can't be bound: the optional ones don't keep it from running."""
        required = {need.family for need in provider.peripheral.needs if need.is_required}
        return "; ".join(why for family, why in self.of(provider.name).items() if family in required)

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


def _waiting(reason: str = "") -> PeripheralStatus:
    return PeripheralStatus(PeripheralState.WAITING, reason)


def _view(peripheral: Peripheral, bound: Mapping[str, list[Binding]]) -> dict[str, Bound]:
    many = {need.family for need in peripheral.needs if need.is_many}
    return {family: [e for _, e in b] if family in many else b[0][1] for family, b in bound.items()}


def _key(bound: Mapping[str, list[Binding]]) -> _Key:
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
    "DisabledStore",
    "MemoryDisabledStore",
    "RETRY_DELAYS",
]
