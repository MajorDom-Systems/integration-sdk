"""Run peripherals by one rule and bind their needs — what the runtime is given and what those bring along — then keep
it in sync as sticks come and go, providers change, and the user enables, disables or restarts them."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from majordom_integration_sdk.controller.platform import Context
from majordom_integration_sdk.peripherals import (
    Bound,
    Endpoint,
    Need,
    Peripheral,
    PeripheralChange,
    Peripherals,
    Service,
)
from majordom_integration_sdk.runtime.peripherals.services.compose import Compose

log = logging.getLogger(__name__)

RETRY_DELAYS = (5, 30, 120)  # seconds before each new start of a peripheral that failed, then it is given up on
STOP_TIMEOUT = 15  # a stop stuck on a vanished radio (zigpy awaiting its dead I/O thread) is abandoned
_PASSES = 10  # bound on the passes one change takes to settle: each may change what the next one binds

_is_driving: ContextVar[bool] = ContextVar("is_driving", default=False)  # inside a call the runtime makes


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
    is_ready: bool = False  # it runs and serves: what needs it may start


class RuntimeOutput(Protocol):
    """What the runtime reports to its owner (the Hub), delegate style like `PeripheralOutput`. Called synchronously
    from the runtime's loop, in order: don't block, schedule what takes time."""

    def peripheral_did_change_status(self, peripheral: Peripheral, status: PeripheralStatus): ...

    def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        """`Context` fields it read from what it runs (a border router's Thread network): merge them into the context
        the runtime is given, then tell it (`Runtime.context_did_change`)."""
        ...


class DisabledStore(Protocol):
    """Where the user's disabling of a peripheral is kept, by name: the Hub's enable/disable store."""

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


type Binding = tuple[Peripheral, Endpoint]  # who provides it, and what


@dataclass(eq=False)
class _Entry:
    peripheral: Peripheral
    contributor: str | None  # the name of what brought it along
    state: PeripheralState
    reason: str = ""
    is_ready: bool = False
    attempts: int = 0  # failures in a row
    retry_at: float = 0.0  # monotonic: a waiting one starts no earlier
    failure: str = ""  # why, and when it is tried again
    bindings: dict[str, list[Binding]] = field(default_factory=dict)  # what it was started or last told with
    task: asyncio.Task | None = None  # its start
    reported: PeripheralStatus | None = None

    @property
    def name(self) -> str:
        return self.peripheral.name

    @property
    def status(self) -> PeripheralStatus:
        return PeripheralStatus(self.state, self.reason, self.is_ready)

    @property
    def is_started(self) -> bool:
        return self.state in (PeripheralState.STARTING, PeripheralState.RUNNING)


class Runtime:
    """Runs every peripheral it is given, and what those bring along, by one rule (see `Peripheral`); binds their
    needs, and is the `PeripheralOutput` of each::

        runtime = Runtime(data, peripherals=[*DEFAULT_PERIPHERALS, HueController(platform)])
        await runtime.start()

    ``peripherals`` are bound in this order, what each brings along right after it: one listed first wins a single-use
    endpoint (the Bluetooth router takes every adapter before the containers) and is preferred for a need (list
    `Pinned` first). ``context`` gives the Hub-wide facts its containers render; ``output`` hears every change of
    status and what is learned; ``disabled`` keeps what the user disabled (in memory by default). It runs the
    containers of the `Service`s through Docker Compose (`Compose`); without ``is_docker_enabled`` it only writes their
    compose file, for you to run. Its own state — the settings, the user's pins and reservations, the containers' ports
    — goes to ``data``.

    A peripheral that isn't disabled or failed runs while what brought it along does, while something that runs binds
    it if it ``is_on_demand``, and once every required need is bound to a provider that runs and is ready. Only a
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
        is_docker_enabled: bool = True,
    ):
        self.data, self.given, self.context, self.output = data, list(peripherals), context, output
        self.disabled = disabled or MemoryDisabledStore()
        self.state: dict[str, Any] = {"settings": {}, "pins": {}, "reserved": {}, "ports": {}}
        with contextlib.suppress(FileNotFoundError):
            saved = json.loads((data / "state.json").read_text())
            self.state |= {key: saved[key] for key in saved.keys() & self.state.keys()}  # older keys start fresh
        self.compose = Compose(data, self.state["ports"], self._save, context, self._learned, is_docker_enabled)
        self._entries: dict[str, _Entry] = {}  # in binding order
        self._wanted: set[str] = set()  # the ones that should run
        self._bound: dict[str, dict[str, list[Binding]]] = {}
        self._unbound: dict[str, dict[str, str]] = {}  # name -> family -> why its need isn't bound
        self._lock = asyncio.Lock()
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
        async with self._lock:
            with self._driving():
                for entry in reversed(self._entries.values()):
                    if entry.is_started:
                        await self._stop(entry, _waiting())
        for task in self._tasks:
            task.cancel()

    async def refresh(self):
        """Collect, bind and run everything anew now."""
        if not self._is_running:
            return
        async with self._lock:
            with self._driving():
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
        """Every peripheral it runs, in binding order: what it was given and what those brought along."""
        return [entry.peripheral for entry in self._entries.values()]

    def peripheral(self, name: str) -> Peripheral:
        return self._entries[name].peripheral

    def status(self, peripheral: Peripheral) -> PeripheralStatus:
        return self._entry(peripheral).status

    def bound(self, peripheral: Peripheral) -> dict[str, Bound]:
        """What each need is bound to, by family: an `Endpoint`, a list of them for an ``is_many`` need."""
        return _view(peripheral, self._bound.get(peripheral.name, {}))

    def providers(self, peripheral: Peripheral) -> dict[str, str]:
        """Who serves each bound need, by family; several, comma-separated, for an ``is_many`` need."""
        bound = self._bound.get(peripheral.name, {})
        return {family: ", ".join(p.name for p, _ in bindings) for family, bindings in bound.items()}

    def unbound(self, peripheral: Peripheral) -> dict[str, str]:
        """Why each need isn't bound, by family: for the user, with what to do when there is something, e.g.
        'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'."""
        return dict(self._unbound.get(peripheral.name, {}))

    def users(self, peripheral: Peripheral) -> list[str]:
        """Every peripheral that runs on it, directly or through others: a radio's integrations and their containers."""
        users: list[str] = []
        providers = {peripheral.name}
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

    def reach(self, endpoints: Iterable[Endpoint]) -> list[str]:
        """The families ``endpoints`` would serve, themselves or through the on-demand peripherals here they would run,
        that something running anyway needs, or what it brings along (an integration, its containers): what a radio's
        firmware is good for."""
        families: set[str] = set()
        reached = {e.family for e in endpoints}
        while reached != families:
            families = reached
            reached = families.union(
                *(
                    {e.family for e in entry.peripheral.provides}
                    for entry in self._entries.values()
                    if entry.peripheral.is_on_demand and _satisfied(entry.peripheral.needs, families)
                )
            )
        anyway = [entry.peripheral for entry in self._entries.values() if self._is_run_anyway(entry)]
        return sorted(families & {need.family for peripheral in anyway for need in peripheral.needs})

    # What the Hub (or you) changes

    async def enable(self, peripheral: Peripheral):
        self.disabled.set_disabled(peripheral.name, False)
        entry = self._entry(peripheral)

        async def apply():
            if entry.state == PeripheralState.DISABLED:
                entry.state, entry.attempts, entry.retry_at = PeripheralState.WAITING, 0, 0.0

        await self._act(apply)

    async def disable(self, peripheral: Peripheral):
        """Stop it, and what it brought along, until enabled again; kept across restarts."""
        self.disabled.set_disabled(peripheral.name, True)
        await self._act(lambda: self._stop(self._entry(peripheral), PeripheralStatus(PeripheralState.DISABLED)))

    async def restart(self, peripheral: Peripheral):
        """Stop it if it runs and start it again, a failed one too: its retries start over. Called from inside a
        peripheral's own start, stop or hook, it happens once that call returns."""
        entry = self._entry(peripheral)
        if entry.state == PeripheralState.DISABLED:
            raise ValueError(f"{peripheral.name} is disabled: enable it")

        async def apply():
            await self._stop(entry, _waiting())
            entry.attempts, entry.retry_at = 0, 0.0

        await self._act(apply)

    async def update_settings(self, peripheral: Peripheral, values: Mapping[str, Any]) -> Any:
        """Validate and keep new values of its ``Settings``; one that runs hears it (`settings_did_change`), a failed
        one tries again."""
        entry = self._entry(peripheral)
        if (model := peripheral.Settings) is None:
            raise KeyError(f"{peripheral.name} has no settings")
        before = peripheral.settings
        peripheral.settings = model.model_validate(before.model_dump() | dict(values))
        self.state["settings"][peripheral.name] = peripheral.settings.model_dump(mode="json")
        self._save()

        async def apply():
            if entry.state == PeripheralState.FAILED:
                entry.state, entry.attempts = PeripheralState.WAITING, 0
            elif entry.state == PeripheralState.RUNNING:
                await self._ask(entry, peripheral.settings_did_change(before))

        await self._act(apply)
        return peripheral.settings

    async def context_did_change(self, before: Context):
        """The Hub-wide facts changed: every peripheral that runs hears it (`Peripheral.context_did_change`)."""

        async def apply():
            for entry in list(self._entries.values()):
                if entry.state == PeripheralState.RUNNING:
                    await self._ask(entry, entry.peripheral.context_did_change(before))

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

    async def peripheral_did_change_provided(self, peripheral: Peripheral):
        await self._settle()

    async def peripheral_did_become_ready(self, peripheral: Peripheral):
        entry = self._entry(peripheral)
        entry.is_ready = entry.is_ready or entry.is_started
        await self._settle()

    async def peripheral_did_fail(self, peripheral: Peripheral, reason: str):
        entry = self._entry(peripheral)

        async def apply():
            if entry.is_started:
                await self._stop(entry, PeripheralStatus(PeripheralState.FAILED, reason))
                entry.attempts = 0

        await self._act(apply)

    # Internals

    def _entry(self, peripheral: Peripheral) -> _Entry:
        entry = self._entries.get(peripheral.name)
        if entry is None or entry.peripheral is not peripheral:
            raise LookupError(f"{peripheral.name} isn't run here")
        return entry

    async def _act(self, change: Callable[[], Awaitable[Any]]):
        """Make a change and settle; from inside a call the runtime makes (it holds the lock), once that returns."""
        if _is_driving.get():
            self._spawn(self._act_unlocked(change))
            return
        await self._act_unlocked(change)

    async def _act_unlocked(self, change: Callable[[], Awaitable[Any]]):
        async with self._lock:
            with self._driving():
                await change()
        await self.refresh()

    async def _settle(self):
        """Bind and run anew: now, or from inside a call the runtime makes, once that returns."""
        if _is_driving.get():
            self._spawn(self.refresh())
        else:
            await self.refresh()

    @contextlib.contextmanager
    def _driving(self):
        token = _is_driving.set(True)
        try:
            yield
        finally:
            _is_driving.reset(token)

    async def _pass(self) -> bool:
        """One round: collect what there is, bind it, stop what shouldn't run, tell what runs what moved, start what
        can. True when something changed that the next round may bind differently."""
        before = self._snapshot()
        is_changed = False
        for entry in self._collect():  # gone: what brought it along let go of it
            is_changed = True
            await self._stop(entry, _waiting())
        self._resolve()
        for entry in reversed(self._entries.values()):  # what uses something stops before it
            if entry.is_started and entry.name not in self._wanted:
                is_changed = True
                await self._stop(entry, _waiting())
        for entry in list(self._entries.values()):
            bound = self._bound.get(entry.name, {})
            if entry.state == PeripheralState.RUNNING and _key(bound) != _key(entry.bindings):
                is_changed = True
                await self._deliver(entry, bound)
            elif entry.state in (PeripheralState.WAITING, PeripheralState.FAILED, PeripheralState.DISABLED):
                self._bind_view(entry, bound)
        for entry in self._entries.values():
            if entry.state == PeripheralState.WAITING:
                entry.reason = self._why_waiting(entry)
                if not entry.reason:
                    self._start(entry)
        return is_changed or self._snapshot() != before

    def _snapshot(self) -> list[tuple[str, tuple[Endpoint, ...], tuple[str, ...]]]:
        return [
            (entry.name, tuple(entry.peripheral.provides), tuple(p.name for p in entry.peripheral.peripherals))
            for entry in self._entries.values()
        ]

    def _collect(self) -> list[_Entry]:
        """Every peripheral given and brought along, in binding order; the ones gone are returned."""
        entries: dict[str, _Entry] = {}
        gone: list[_Entry] = []

        def walk(peripheral: Peripheral, contributor: str | None):
            if (seen := entries.get(peripheral.name)) is not None:
                if seen.peripheral is not peripheral:
                    log.error(f"Two peripherals are named {peripheral.name}: the second is left out")
                return
            entry = self._entries.get(peripheral.name)
            if entry is None or entry.peripheral is not peripheral:
                if entry is not None:
                    gone.append(entry)  # replaced by another of the same name
                entry = self._adopt(peripheral, contributor)
            entries[peripheral.name] = entry
            for child in peripheral.peripherals:
                walk(child, peripheral.name)

        for peripheral in self.given:
            walk(peripheral, None)
        gone += [entry for name, entry in self._entries.items() if name not in entries]
        self._entries = entries
        return gone

    def _adopt(self, peripheral: Peripheral, contributor: str | None) -> _Entry:
        peripheral.output = self
        if isinstance(peripheral, Service):
            peripheral.host = self.compose
        peripheral.settings = self._settings(peripheral)
        is_disabled = self.disabled.is_disabled(peripheral.name)
        return _Entry(peripheral, contributor, PeripheralState.DISABLED if is_disabled else PeripheralState.WAITING)

    def _settings(self, peripheral: Peripheral) -> Any:
        if (model := peripheral.Settings) is None:
            return None
        stored = self.state["settings"].get(peripheral.name, {})
        current = model.model_validate(stored)
        if (dumped := current.model_dump(mode="json")) != stored:  # freeze generated defaults (e.g. keys)
            self.state["settings"][peripheral.name] = dumped
            self._save()
        return current

    def _is_run_anyway(self, entry: _Entry) -> bool:
        """It, or what brought it along, runs whether something uses it or not."""
        while entry.peripheral.is_on_demand:
            if entry.contributor is None:
                return False
            entry = self._entries[entry.contributor]
        return True

    def _is_alive(self, entry: _Entry) -> bool:
        """Neither it nor what brought it along is disabled or failed."""
        while True:
            if entry.state in (PeripheralState.DISABLED, PeripheralState.FAILED):
                return False
            if entry.contributor is None:
                return True
            entry = self._entries[entry.contributor]

    def _resolve(self):
        """The ones that should run, as the greatest set where each one alive is bound by another of the set if it
        is on demand and has every required need bound to the set; binding in order, single-use endpoints taken."""
        alive = {name for name, entry in self._entries.items() if self._is_alive(entry)}
        provides = {name: list(entry.peripheral.provides) for name, entry in self._entries.items()}
        wanted = set(alive)
        while True:
            bound, taken = self._bind(wanted, provides)
            if unbound := wanted - bound.keys():  # first: what bound to one of these binds anew
                wanted -= unbound
                continue
            used = {p.name for b in bound.values() for bindings in b.values() for p, _ in bindings}
            if unused := {name for name in wanted if self._entries[name].peripheral.is_on_demand and name not in used}:
                wanted -= unused
                continue
            break
        self._wanted, self._bound = wanted, bound
        self._unbound = _Reasons(self, alive, wanted, provides, taken).all()

    def _bind(
        self, wanted: set[str], provides: Mapping[str, list[Endpoint]]
    ) -> tuple[dict[str, dict[str, list[Binding]]], dict[tuple[str, str, str], str]]:
        """Bind the needs of every wanted one in order; one whose required need finds nothing is left out."""
        bound: dict[str, dict[str, list[Binding]]] = {}
        taken: dict[tuple[str, str, str], str] = {}  # (provider, family, url) of a single-use endpoint -> who took it
        for name in [name for name in self._entries if name in wanted]:
            owner, mine, bindings = self._entries[name].peripheral, [], {}
            for need in owner.needs:
                chosen = [
                    (provider, endpoint)
                    for provider, endpoint in self._candidates(name, need, wanted, provides)
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
            (self._entries[name].peripheral, endpoint)
            for name in self._entries
            if name in among and name != owner
            for endpoint in provides[name]
            if need.accepts(endpoint)
        ]
        pin = self.state["pins"].get(need.family)
        reserved = self.state["reserved"].get(owner, {})
        return sorted(candidates, key=lambda c: (reserved.get(c[1].family) != c[1].url, c[0].name != pin))

    def _is_free(
        self, provider: Peripheral, endpoint: Endpoint, owner: str, taken: Mapping[tuple[str, str, str], str]
    ) -> bool:
        """Whether ``owner`` may bind ``endpoint``: no one took it, and it isn't reserved for another."""
        reserved = self.state["reserved"].items()
        is_kept = any(kept.get(endpoint.family) == endpoint.url and name != owner for name, kept in reserved)
        return not is_kept and (provider.name, endpoint.family, endpoint.url) not in taken

    def _why_waiting(self, entry: _Entry) -> str:
        """Why a waiting one doesn't start now; empty when it can."""
        if (contributor := entry.contributor) is not None and not self._is_alive(self._entries[contributor]):
            return f"comes with {contributor}, which is {self._entries[contributor].state}"
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
        return entry.state == PeripheralState.RUNNING and entry.is_ready

    def _bind_view(self, entry: _Entry, bound: Mapping[str, list[Binding]]):
        if isinstance(view := entry.peripheral.peripherals, Peripherals):
            view.bind(_view(entry.peripheral, bound), restart=self.restart)

    def _start(self, entry: _Entry):
        entry.state, entry.reason, entry.is_ready = PeripheralState.STARTING, "", False
        entry.bindings = self._bound[entry.name]
        self._bind_view(entry, entry.bindings)
        entry.peripheral.settings = self._settings(entry.peripheral)
        entry.task = self._spawn(self._run_start(entry))

    async def _run_start(self, entry: _Entry):
        peripheral = entry.peripheral
        try:
            with self._driving():
                await peripheral.start()
        except Exception as error:
            log.error(f"{peripheral.name} failed to start: {error}", exc_info=error)
            async with self._lock:
                entry.task = None
                if entry.state == PeripheralState.STARTING:
                    self._fail(entry, str(error) or type(error).__name__)
        else:
            async with self._lock:
                entry.task = None
                if entry.state == PeripheralState.STARTING:
                    entry.state, entry.attempts = PeripheralState.RUNNING, 0
                    entry.is_ready = entry.is_ready or peripheral.is_ready_on_start
        await self.refresh()

    def _fail(self, entry: _Entry, reason: str):
        """Count a failure: wait to try again, or give up once the retries are spent."""
        entry.attempts += 1
        if entry.attempts > len(RETRY_DELAYS):
            entry.state, entry.reason, entry.attempts = PeripheralState.FAILED, reason, 0
            return
        delay = RETRY_DELAYS[entry.attempts - 1]
        entry.state, entry.retry_at = PeripheralState.WAITING, monotonic() + delay
        entry.reason = entry.failure = f"{reason}; trying again in {delay} s"

    async def _stop(self, entry: _Entry, status: PeripheralStatus):
        """Stop it if it runs or starts, and leave it in ``status``."""
        if entry.state == PeripheralState.DISABLED and status.state != PeripheralState.DISABLED:
            return  # only enabling ends it
        is_started = entry.is_started
        if entry.task:
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)
            entry.task = None
        if is_started:
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    await entry.peripheral.stop()
            except Exception as error:
                log.error(f"{entry.name} failed to stop cleanly: {error}", exc_info=error)
        entry.state, entry.reason, entry.is_ready, entry.bindings = status.state, status.reason, False, {}

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
        try:
            if await hook:
                return True
            await self._stop(entry, _waiting())
        except Exception as error:
            log.error(f"{entry.name} failed to take a change: {error}", exc_info=error)
            await self._stop(entry, _waiting())
            self._fail(entry, str(error) or type(error).__name__)
        return False

    def _report(self):
        for entry in self._entries.values():
            if entry.reported != (status := entry.status):
                entry.reported = status
                if self.output:
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

    def _learned(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        if self.output:
            self.output.peripheral_did_learn(peripheral, facts)

    def _save(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "state.json").write_text(json.dumps(self.state, indent=2))

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
            elif provider.name not in self.wanted and (why := "; ".join(self.of(provider.name).values())):
                rejected.append(f"{provider.name} can't run ({why})")
            else:
                return None  # free, or on demand and idle only because the owner doesn't run
        wanted = f"{need.family}/{'|'.join(need.dialects) or '*'}"
        return f"need {wanted}: {'; '.join(dict.fromkeys(rejected)) or 'nothing offers it'}"

    def hint(self, taker: str, family: str) -> str:
        """What to need instead when what took the endpoint offers the same family itself: the router for raw BLE."""
        endpoint = next((e for e in self.provides.get(taker, []) if e.family == family), None)
        return f' — use Need("{family}", ["{endpoint.dialect}"])' if endpoint else ""


def _waiting(reason: str = "") -> PeripheralStatus:
    return PeripheralStatus(PeripheralState.WAITING, reason)


def _view(peripheral: Peripheral, bound: Mapping[str, list[Binding]]) -> dict[str, Bound]:
    many = {need.family for need in peripheral.needs if need.is_many}
    return {family: [e for _, e in b] if family in many else b[0][1] for family, b in bound.items()}


def _key(bound: Mapping[str, list[Binding]]) -> dict[str, list[tuple[Peripheral, Endpoint, Any]]]:
    """What tells two bindings apart: the provider, the endpoint and the very instance it carries."""
    return {family: [(p, e, id(e.instance)) for p, e in bindings] for family, bindings in bound.items()}


def _satisfied(needs: Sequence[Need], families: set[str]) -> bool:
    return all(need.family in families for need in needs if need.is_required)


__all__ = [
    "Runtime",
    "RuntimeOutput",
    "PeripheralState",
    "PeripheralStatus",
    "DisabledStore",
    "MemoryDisabledStore",
    "RETRY_DELAYS",
]
