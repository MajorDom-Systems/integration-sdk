"""Find radios, bind every consumer's needs to what radios, services, the host, the runtime's own providers and the
consumers themselves offer, and run the services that end up bound through Docker Compose — then keep it in sync as
sticks come and go and offers change."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import logging
import os
import socket
import string
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from glob import glob
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from majordom_integration_sdk.controller.platform import Context, Platform
from majordom_integration_sdk.peripherals import Bound, Endpoint, Need, PeripheralChange, Peripherals, Service
from majordom_integration_sdk.runtime.peripherals.hardware.drivers import DRIVERS, RADIO_USB_IDS
from majordom_integration_sdk.runtime.peripherals.providers import Provider, builtin_providers
from majordom_integration_sdk.runtime.peripherals.providers.ble import cpc_hci
from majordom_integration_sdk.runtime.peripherals.services.builtin import SERVICES
from majordom_integration_sdk.schemas.credentials import WifiCredentials

log = logging.getLogger(__name__)

PORTS = range(45800, 46000)  # where a service goes when the port it prefers is taken
PROBE_TIMEOUT = 60  # per driver or integration probe: one that hangs (bellows resetting a busy port) is given up


class Consumer(Protocol):
    """Anything shaped like an integration controller class."""

    needs: Sequence[Need]
    services: Sequence[Service]
    settings: type[BaseModel] | None

    async def probe(self, path: str) -> Endpoint | None: ...


#: Radio regulatory regions, named as Z-Wave JS names them.
Region = Literal[
    "Europe", "USA", "Australia/New Zealand", "Hong Kong", "India", "Israel", "Russia", "China", "Japan", "Korea",
    "USA (Long Range)", "Europe (Long Range)",
]  # fmt: skip


class PlatformSettings(BaseModel):
    region: Region | None = Field(None, title="Radio region", description="Where the hub is used.")
    wifi: list[WifiCredentials] = Field([], title="Wi-Fi networks", description="Saved networks to set devices up on.")
    wifi_ssid: str | None = Field(None, title="Wi-Fi for devices", description="Empty uses the hub's own network.")
    firmware: Literal["stable", "dev"] = Field(
        "stable", title="Radio firmware", description="Stable offers verified builds only, dev every known build."
    )


@dataclass
class Radio:
    id: str
    path: str
    present: bool
    driver: str | None  # None: recognised by an integration's probe, or not recognised at all
    firmware: str | None
    offers: list[str]  # what it serves right now, through services: zigbee, thread, …
    options: dict[str, list[str]]  # firmware it can be switched to -> families that firmware would serve
    used_by: list[str] = field(default_factory=list)
    busy: bool = False  # being flashed
    held_by: str | None = None  # another program has it open: left alone until claimed (probe, or a pin)


type Binding = tuple[str, Endpoint]  # who provides it, and what


@dataclass(frozen=True)
class _ServiceControl:
    """A consumer's hold on the services it brings: restarting one leaves the consumers running."""

    runtime: Runtime
    own: frozenset[str]

    async def restart(self, service: str) -> None:
        if service not in self.own:
            raise PermissionError(f"{service} isn't one of this integration's services")
        await self.runtime.restart(service)


@dataclass(frozen=True)
class _Offers:
    """A consumer's offers, bound to its name."""

    runtime: Runtime
    owner: str

    async def offer(self, endpoint: Endpoint) -> None:
        await self.runtime.offer(self.owner, endpoint)

    async def withdraw(self, endpoint: Endpoint) -> None:
        await self.runtime.withdraw(self.owner, endpoint)


class Runtime:
    def __init__(
        self,
        data: Path,
        consumers: Mapping[str, Consumer] = {},
        *,
        docker: bool = True,
        scan: bool = True,
        devices: Mapping[str, str] = {},
        board: Sequence[Mapping[str, str]] = (),
        pinned: Mapping[str, Endpoint] = {},
        default_wifi: Callable[[], WifiCredentials | None] = lambda: None,
        on_change: Callable[[], Awaitable[None]] | None = None,
        poll: float = 2,
        builtins: Sequence[Provider] | None = None,
    ):
        """
        consumers: integration controller classes by slug, bound in this order after the providers.
        docker: run the bound services; False leaves them to you (pin their endpoints).
        scan: look for USB sticks in /dev/serial/by-id; `devices` adds paths by id.
        board: soldered radios, ``{"id", "path", "driver", "firmware"}``, used without asking; ``"params"``
            overrides the firmware's serial settings where the board wires its UART differently.
        pinned: endpoints you provide yourself by family; they win over anything found.
        builtins: the peripherals run in this process (the Bluetooth router, DNS-SD, SSDP); None runs one of each
            whose extra is installed.
        """
        self.data, self.consumers, self.docker, self.scan = data, dict(consumers), docker, scan
        self.devices, self.board, self.pinned = dict(devices), list(board), dict(pinned)
        self.default_wifi, self.on_change, self.poll = default_wifi, on_change, poll
        self.builtins = {p.name: p for p in (builtin_providers() if builtins is None else builtins)}
        self.services = {s.name: s for c in self.consumers.values() for s in c.services} | {s.name: s for s in SERVICES}
        self.state: dict[str, Any] = {"radios": {}, "pins": {}, "reserved": {}, "settings": {}, "ports": {}}
        with contextlib.suppress(FileNotFoundError):
            self.state |= json.loads((data / "state.json").read_text())
        for radio in self.board:
            self.state["radios"].setdefault(radio["id"], dict(radio))
        self._bound: dict[str, dict[str, list[Binding]]] = {}  # provider, consumer or service -> family -> bindings
        self._unbound: dict[str, dict[str, str]] = {}  # owner -> family -> why its need isn't bound
        self._offered: dict[str, list[Endpoint]] = {}  # consumer -> what it offers right now
        self._present: dict[str, str] = {}
        self._nodes: dict[str, str] = {}  # radio id -> device node its path resolves to (a ttyUSB*)
        self._busy: set[str] = set()  # radios being flashed
        self._held: dict[str, str] = {}  # radio id -> the other program that has it open
        self._compose = ""
        self._learned: dict[tuple[str, str], Mapping[str, Any]] = {}  # bound (provider, url) -> context facts
        self._tasks: dict[tuple[str, str], list[asyncio.Task]] = {}  # services setting up and learning, by source
        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task | None = None
        self._notifying: set[asyncio.Task] = set()

    # Lifecycle

    async def start(self):
        for provider in self.builtins.values():
            try:
                await provider.start()
            except Exception:
                log.exception(f"{provider.name} failed to start")
        await self.refresh()
        self._watcher = asyncio.create_task(self._watch(), name="runtime.watch")

    async def stop(self):
        for task in (self._watcher, *self._notifying, *(t for tasks in self._tasks.values() for t in tasks)):
            if task:
                task.cancel()
        for provider in self.builtins.values():
            try:
                await provider.stop()
            except Exception:
                log.exception(f"{provider.name} failed to stop")

    async def refresh(self):
        async with self._lock:
            self._present = self._find()
            self._nodes = _nodes(self._present)
            self._held = {}
            for radio_id, path in self._present.items():
                known, trusted = radio_id in self.state["radios"], self._trusted(radio_id, path)
                if holder := self._holder(radio_id, open_it=not known and trusted):
                    self._held[radio_id] = holder
                if not known and (holder or radio_id in self._busy):
                    continue  # identified once the other program lets go of it, or by the probe that holds it
                if not known:
                    self.state["radios"][radio_id] = await self._identify(path) if trusted else {}
                self.state["radios"][radio_id]["path"] = path
                await self._check_bluetooth(self.state["radios"][radio_id])
            self._save()
            await self._rebind()
        if self.on_change:
            await self.on_change()

    async def _rebind(self):
        """Bind every need again, run what is bound, and tell the providers what they got."""
        before = {name: self._peripherals(name) for name in self.builtins}
        self._resolve()
        await self._allocate()
        recreated = await self._apply()
        self._follow(recreated)
        for name, provider in self.builtins.items():
            was, now = before[name] or Peripherals(), self._peripherals(name) or Peripherals()
            for family in sorted(was.keys() | now.keys()):
                if was.get(family) != now.get(family):
                    try:
                        await provider.peripheral_did_change(PeripheralChange(family, was.get(family), now.get(family)))
                    except Exception:
                        log.exception(f"{name} failed to take its {family} peripherals")

    async def _check_bluetooth(self, entry: dict[str, Any]):
        """Whether a multi-PAN radio's firmware serves Bluetooth too: the driver's firmware metadata says, else the
        radio is asked once."""
        driver = DRIVERS.get(entry.get("driver", ""))
        firmware = entry.get("firmware")
        if (
            not driver
            or "bluetooth" in entry
            or not any(e.family == "multipan" for e in fw_endpoints(driver, firmware, ""))
        ):
            return
        entry["bluetooth"] = firmware in driver.bluetooth or await cpc_hci.has_bluetooth(entry["path"])

    def _follow(self, recreated: bool):
        """Have services set up what runs and learn facts from what is bound; known facts stay until read
        again, and go with their provider."""
        sources = self._sources()
        for key in [key for key in self._tasks if recreated or key not in sources]:
            for task in self._tasks.pop(key):
                task.cancel()
        self._learned = {key: facts for key, facts in self._learned.items() if key in sources}
        for key, (service, endpoint) in sources.items():
            running = any(not task.done() for task in self._tasks.get(key, []))
            if recreated or key not in self._learned and not running:  # a source unchanged keeps setting up
                self._tasks[key] = [asyncio.create_task(self._learn(key, service, endpoint))]
                if key[0] == service.name:  # never someone else's (pinned)
                    settings = self.settings(service.name)
                    self._tasks[key].append(asyncio.create_task(self._provision(service, endpoint, settings)))

    async def _learn(self, key: tuple[str, str], service: Service, endpoint: Endpoint):
        try:
            facts = dict(await service.learn(endpoint))
        except Exception:
            log.exception(f"{service.name} failed to learn from {endpoint.url}")
            return
        before = self._facts()
        self._learned[key] = facts
        if self._facts() != before and self.on_change:
            await self.on_change()

    @staticmethod
    async def _provision(service: Service, endpoint: Endpoint, settings: BaseModel | None):
        try:
            await service.provision(endpoint, settings)
        except Exception:
            log.exception(f"{service.name} failed to set up {endpoint.url}")

    def _facts(self) -> dict[str, Any]:
        return {name: value for facts in self._learned.values() for name, value in facts.items()}

    async def _watch(self):
        seen = self._nodes
        while True:
            await asyncio.sleep(self.poll)
            found = _nodes(self._find())
            if found != seen:  # act once the change held for one more poll (a stick re-enumerating)
                seen = found
            elif found != self._nodes or any(not self._holder(i, i not in self.state["radios"]) for i in self._held):
                try:
                    await self.refresh()
                except Exception:
                    log.exception("Refreshing radios failed")

    # What the hub (or you) reads

    def peripherals(self, consumer: str) -> Peripherals | None:
        """Bound needs of a consumer by family; None while a required need can't be met (see `unbound`)."""
        return self._peripherals(consumer)

    def providers(self, owner: str) -> dict[str, str]:
        """Who serves each bound need of a consumer, service or provider: a radio id, a service or provider name, a
        consumer that offered it, "host" or "pinned"; several, comma-separated, for a ``many`` need."""
        return {family: ", ".join(p for p, _ in bound) for family, bound in self._bound.get(owner, {}).items()}

    def unbound(self, owner: str) -> dict[str, str]:
        """Why each need of a consumer, service or provider isn't bound, by family: for the user, with what to do
        when there is something, e.g. 'need ble/bluez: every provider taken by ble-router — use Need("ble",
        ["router"])'."""
        return dict(self._unbound.get(owner, {}))

    def platform(self, consumer: str, base: Platform) -> Platform:
        """``base`` with what the runtime holds for ``consumer``: its settings, the context, its services and offers."""
        services = _ServiceControl(self, frozenset(s.name for s in self.consumers[consumer].services))
        settings, context = self.settings(consumer), self.context()
        return base.copy(settings=settings, context=context, services=services, offers=_Offers(self, consumer))

    def context(self) -> Context:
        hub = self.settings("hub")
        wifi = next((w for w in hub.wifi if w.ssid == hub.wifi_ssid), None) or self.default_wifi()
        return Context(region=hub.region, wifi=wifi, **self._facts())

    def radios(self) -> list[Radio]:
        result = []
        unknown = {radio_id: {"path": self._present[radio_id]} for radio_id in self._held}
        for radio_id, entry in (unknown | self.state["radios"]).items():
            path, driver = entry.get("path", ""), DRIVERS.get(entry.get("driver", ""))
            firmwares = driver.flashable(path, self.settings("hub").firmware) if driver else []
            result.append(
                Radio(
                    id=radio_id,
                    path=path,
                    present=radio_id in self._present,
                    driver=entry.get("driver"),
                    firmware=entry.get("firmware"),
                    offers=self._reach(self._radio_endpoints(radio_id, entry)),
                    options={fw: self._reach(fw_endpoints(driver, fw, path)) for fw in firmwares},
                    used_by=[c for c in self.consumers if radio_id in self._roots(c)],
                    busy=radio_id in self._busy,
                    held_by=self._held.get(radio_id),
                )
            )
        return result

    def settings(self, scope: str) -> Any:
        """Validated settings of a consumer (slug), a service (name) or the platform ("hub"); None if it has none."""
        owner = self.consumers.get(scope) or self.services.get(scope)
        model = PlatformSettings if scope == "hub" else getattr(owner, "settings", None)
        if model is None:
            return None
        stored = self.state["settings"].get(scope, {})
        current = model.model_validate(stored)
        if (dumped := current.model_dump(mode="json")) != stored:  # freeze generated defaults (e.g. keys)
            self.state["settings"][scope] = dumped
            self._save()
        return current

    def settings_scopes(self, consumer: str) -> list[str]:
        """The consumer itself and every service in its chain that has settings — shown together to the user."""
        scopes = [consumer, *sorted(self._roots(consumer) & self.services.keys())]
        return [scope for scope in scopes if self.settings(scope) is not None]

    # What the hub (or you) changes

    async def update_settings(self, scope: str, values: Mapping[str, Any]) -> Any:
        model = self.settings(scope)
        if model is None:
            raise KeyError(f"{scope} has no settings")
        updated = type(model).model_validate(model.model_dump() | dict(values))
        self.state["settings"][scope] = updated.model_dump(mode="json")
        self._save()
        await self.refresh()
        return self.settings(scope)

    async def restart(self, service: str):
        """Restart a running service's container; nothing else changes, so integrations aren't told."""
        if service not in self._running_services():
            raise LookupError(f"{service} isn't running")
        if not self.docker:
            return  # you run the services
        async with self._lock:  # not while compose is recreating it
            process = await asyncio.create_subprocess_exec(
                "docker", "compose", "-f", str(self.data / "compose.json"), "restart", service,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            output = (await process.communicate())[0].decode(errors="replace")
        if process.returncode:
            raise RuntimeError(f"Restarting {service} failed:\n{output[-3000:]}")

    async def set_firmware(self, radio_id: str, firmware: str):
        entry = self.state["radios"][radio_id]
        self._busy.add(radio_id)
        try:
            await self.refresh()  # releases the stick: whatever used it is stopped or unlinked
            channel = self.settings("hub").firmware
            await DRIVERS[entry["driver"]].flash(entry["path"], firmware, channel, self.data / "firmware")
            entry["firmware"] = await DRIVERS[entry["driver"]].identify(entry["path"])
            entry.pop("bluetooth", None)
        finally:
            self._busy.discard(radio_id)
        await self.refresh()

    async def probe(self, radio_id: str):
        """Identify a radio now, also one whose USB id isn't a known radio's (its port is opened, which
        may reset what's behind it). Whatever uses it lets go of it first."""
        self._busy.add(radio_id)
        try:
            await self.refresh()
            async with self._lock:
                self.state["radios"][radio_id] = await self._identify(self._present[radio_id]) | {"claimed": True}
        finally:
            self._busy.discard(radio_id)
        await self.refresh()

    async def forget(self, radio_id: str):
        """Drop what is known about a radio; one that is plugged in is identified again."""
        if radio_id in self._present:
            await self.probe(radio_id)
        else:
            self.state["radios"].pop(radio_id, None)
            await self.refresh()

    async def pin(self, family: str, provider: str | None):
        """Prefer ``provider`` (a radio id or service name) for ``family``; None goes back to automatic."""
        if provider is None:
            self.state["pins"].pop(family, None)
        else:
            self.state["pins"][family] = provider
        await self.refresh()

    async def reserve(self, owner: str, family: str, url: str | None):
        """Keep the ``family`` endpoint at ``url`` for ``owner`` alone (a consumer or a service), bound to it before
        anyone else can bind it, the providers' ``many`` needs included: one Bluetooth adapter, ``ble/bluez hci1``, for
        matterjs-server to use directly. None gives it back."""
        if url is None:
            self.state["reserved"].get(owner, {}).pop(family, None)
        else:
            self.state["reserved"].setdefault(owner, {})[family] = url
        await self.refresh()

    async def pin_endpoint(self, endpoint: Endpoint | None, family: str | None = None):
        """Provide ``endpoint`` yourself for its family, winning over anything found (a ``socket://`` EZSP stick, a
        matter-server you run); None with ``family`` drops yours."""
        if endpoint is None:
            self.pinned.pop(family or "", None)
        else:
            self.pinned[endpoint.family] = endpoint
        await self.refresh()

    async def offer(self, owner: str, endpoint: Endpoint):
        """Offer ``endpoint`` on behalf of ``owner`` while it runs, e.g. ESPHome lending a node's Bluetooth as
        ``ble/esphome-proxy``; one with the same family and url is replaced."""
        offered = [e for e in self._offered.get(owner, []) if (e.family, e.url) != (endpoint.family, endpoint.url)]
        self._offered[owner] = [*offered, endpoint]
        await self._reoffer()

    async def withdraw(self, owner: str, endpoint: Endpoint):
        offered = self._offered.get(owner, [])
        self._offered[owner] = [e for e in offered if (e.family, e.url) != (endpoint.family, endpoint.url)]
        await self._reoffer()

    async def _reoffer(self):
        async with self._lock:
            await self._rebind()
        # the offerer may be the one being set up right now: it never waits for itself to be told
        if self.on_change:
            self._spawn(self.on_change())

    # Internals

    def _find(self) -> dict[str, str]:
        found = {Path(p).name: p for p in glob("/dev/serial/by-id/*")} if self.scan else {}
        found |= {r["id"]: r["path"] for r in self.board if Path(r["path"]).exists()}
        return found | {i: p for i, p in self.devices.items() if Path(p).exists()}

    def _holder(self, radio_id: str, open_it: bool) -> str | None:
        """The other program that has a radio open, unless the user claimed it or it is being flashed."""
        claimed = self.state["radios"].get(radio_id, {}).get("claimed") or radio_id in self.state["pins"].values()
        if claimed or radio_id in self._busy or (path := self._present.get(radio_id)) is None:
            return None
        return _holder(path, open_it=open_it)

    def _trusted(self, radio_id: str, path: str) -> bool:
        """Safe to open unasked: given explicitly, soldered on the board, or a known radio's USB id."""
        soldered = any(r["id"] == radio_id for r in self.board)
        return radio_id in self.devices or soldered or _usb_id(path) in RADIO_USB_IDS

    async def _identify(self, path: str) -> dict[str, Any]:
        for name, driver in DRIVERS.items():
            with contextlib.suppress(Exception):
                async with asyncio.timeout(PROBE_TIMEOUT):
                    if firmware := await driver.identify(path):
                        return {"driver": name, "firmware": firmware}
        for consumer in self.consumers.values():
            with contextlib.suppress(Exception):
                async with asyncio.timeout(PROBE_TIMEOUT):
                    if endpoint := await consumer.probe(path):
                        return {"family": endpoint.family, "dialect": endpoint.dialect}
        log.info(f"No driver or integration recognised {path}")
        return {}

    def _radio_endpoints(self, radio_id: str, entry: Mapping[str, Any]) -> list[Endpoint]:
        if radio_id not in self._present or radio_id in self._busy or radio_id in self._held:
            return []
        node = self._nodes.get(radio_id, entry["path"])
        if "family" in entry:
            return [Endpoint(entry["family"], entry["dialect"], node)]
        endpoints = fw_endpoints(
            DRIVERS.get(entry.get("driver", "")), entry.get("firmware"), node, entry.get("params", {})
        )
        if entry.get("bluetooth"):  # computed: the firmware serves Bluetooth over CPC too
            endpoints.append(Endpoint("ble", "silabs-cpc-hci", node, params={"hci": entry.get("hci")}))
        return endpoints

    def _offers(self) -> list[Binding]:
        offers: list[Binding] = [("pinned", e) for e in self.pinned.values()] + [("host", e) for e in _host_endpoints()]
        offers += [(i, e) for i, entry in self.state["radios"].items() for e in self._radio_endpoints(i, entry)]
        offers += [(name, e) for name, s in self.services.items() for e in s.provides]
        offers += [(name, p.endpoint) for name, p in self.builtins.items()]
        offers += [(owner, e) for owner, endpoints in self._offered.items() for e in endpoints]
        pins = self.state["pins"]
        return sorted(offers, key=lambda offer: (offer[0] != "pinned", pins.get(offer[1].family) != offer[0]))

    def _owner_needs(self, owner: str) -> Sequence[Need]:
        holder = self.builtins.get(owner) or self.services.get(owner) or self.consumers.get(owner)
        return holder.needs if holder else ()

    def _peripherals(self, owner: str) -> Peripherals | None:
        if (bound := self._bound.get(owner)) is None:
            return None
        many = {need.family for need in self._owner_needs(owner) if need.many}
        return Peripherals({family: [e for _, e in b] if family in many else b[0][1] for family, b in bound.items()})

    def _running_services(self) -> set[str]:
        return self._bound.keys() & self.services.keys()

    def _resolve(self):
        """Providers first, then the consumers in order, each binding the services it reaches."""
        resolved: dict[str, dict[str, list[Binding]] | None] = {}
        unbound: dict[str, dict[str, str]] = {}
        taken: dict[tuple[str, str, str], str] = {}  # (provider, family, url) of a single-use endpoint -> who took it
        offers = self._offers()
        reserved = {
            (family, url): owner for owner, kept in self.state["reserved"].items() for family, url in kept.items()
        }
        offered_by = {}  # what each provider, service or consumer offers, by family: for the hint
        for provider, endpoint in offers:
            offered_by.setdefault((provider, endpoint.family), endpoint)

        def why(need: Need, rejected: list[str]) -> str:
            wanted = f"{need.family}/{'|'.join(need.dialects) or '*'}"
            return f"need {wanted}: {'; '.join(dict.fromkeys(rejected)) or 'nothing offers it'}"

        def bind(owner: str, needs: Sequence[Need]) -> dict[str, list[Binding]] | None:
            if owner in resolved:
                return resolved[owner]
            resolved[owner] = None  # also guards against cycles
            bound: dict[str, list[Binding]] = {}
            mine: list[tuple[str, str, str]] = []
            failed = False
            for need in needs:
                chosen: list[Binding] = []
                rejected: list[str] = []
                candidates = sorted(offers, key=lambda o: reserved.get((o[1].family, o[1].url)) != owner)
                for provider, endpoint in candidates:
                    if not need.accepts(endpoint) or provider == owner:
                        continue
                    key = (provider, endpoint.family, endpoint.url)
                    if (keeper := reserved.get((endpoint.family, endpoint.url), owner)) != owner:
                        rejected.append(f"{endpoint.url or endpoint.dialect} is reserved for {keeper}")
                        continue
                    if (taker := taken.get(key)) is not None:
                        rejected.append(f"every provider taken by {taker}" + _hint(offered_by, taker, need.family))
                        continue
                    if provider in self.services and bind(provider, self.services[provider].needs) is None:
                        rejected.append(f"{provider} can't run ({'; '.join(unbound.get(provider, {}).values())})")
                        continue
                    if not endpoint.shared:
                        taken[key] = owner
                        mine.append(key)
                    chosen.append((provider, endpoint))
                    if not need.many:
                        break
                if chosen:
                    bound[need.family] = chosen
                    continue
                unbound.setdefault(owner, {})[need.family] = why(need, rejected)
                failed = failed or need.required  # the rest are still looked at: each says why it's unbound
            if failed:
                for key in mine:  # what it took goes back to the others
                    del taken[key]
                return None
            resolved[owner] = bound
            return bound

        for name, provider in self.builtins.items():
            bind(name, provider.needs)
        for name, consumer in self.consumers.items():
            bind(name, consumer.needs)
        self._bound = {o: v for o, v in resolved.items() if v is not None}
        self._unbound = unbound

    async def _allocate(self):
        """Host ports for the bound services, then the links to them. A service keeps its ports across
        restarts; it gets the preferred one when free and moves only when something else took it."""
        allocated: dict[str, dict[str, int]] = self.state["ports"]
        running: set[str] | None = None
        for name in sorted(self._running_services()):
            mine = allocated.setdefault(name, {})
            for port_name, preferred in self.services[name].ports.items():
                others = {p for s, ports in allocated.items() for n, p in ports.items() if (s, n) != (name, port_name)}
                port = mine.get(port_name)
                if port in others:
                    port = None
                elif port and _listening(port):
                    running = await self._running() if running is None else running
                    port = port if name in running else None  # ours holds it, or something else took it
                if not port:
                    port = next(p for p in (preferred, *PORTS) if p not in others and not _listening(p))
                mine[port_name] = port
        self._save()
        for bound in self._bound.values():
            for family, bindings in bound.items():
                bound[family] = [
                    (p, replace(e, url=_render(e.url, self._templates(p))) if p in self.services else e)
                    for p, e in bindings
                ]

    async def _running(self) -> set[str]:
        """Our services whose containers are up."""
        if not self.docker:
            return set()
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "-p", "majordom", "ps", "--status", "running", "--services",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        return set((await process.communicate())[0].decode().split())

    def _templates(self, service: str) -> dict[str, Any]:
        return {"ports": SimpleNamespace(**self.state["ports"].get(service, {}))}

    def _roots(self, owner: str) -> set[str]:
        """Every provider ``owner`` depends on, directly or through services."""
        direct = {p for bindings in self._bound.get(owner, {}).values() for p, _ in bindings}
        return direct.union(*(self._roots(p) for p in direct if p in self.services))

    def _sources(self) -> dict[tuple[str, str], tuple[Service, Endpoint]]:
        """Bound endpoints by (provider, url) with the service that sets them up or reads them: their own,
        or for a pinned one any service providing that kind."""
        readers = {(e.family, e.dialect): s for s in self.services.values() for e in s.provides}
        sources = {}
        for bound in self._bound.values():
            for provider, endpoint in (binding for bindings in bound.values() for binding in bindings):
                kind = readers.get((endpoint.family, endpoint.dialect)) if provider == "pinned" else None
                if service := self.services.get(provider) or kind:
                    sources[provider, endpoint.url] = service, endpoint
        return sources

    def _reach(self, endpoints: list[Endpoint]) -> list[str]:
        """Families a firmware would serve, through services, that some integration asks for."""
        families: set[str] = set()
        reached = {e.family for e in endpoints}
        while reached != families:  # follow services until nothing new is reachable
            families = reached
            reached = families.union(
                *({e.family for e in s.provides} for s in self.services.values() if _satisfied(s.needs, families))
            )
        wanted = {need.family for c in self.consumers.values() for need in c.needs}
        wanted |= {need.family for c in self.consumers.values() for s in c.services for need in s.needs}
        return sorted(families & wanted)

    async def _apply(self) -> bool:
        """Run the bound services; True when what runs changed."""
        services = {}
        for name in self._running_services():
            data = self.data / "services" / name
            data.mkdir(parents=True, exist_ok=True)
            peripherals = _Peripherals(self._peripherals(name) or {})
            values = {"data": data, "peripherals": peripherals, "settings": self.settings(name)}
            values |= {"context": self.context(), "host": _Host()} | self._templates(name)
            # no depends_on: everything retries until its links answer, and a dependency would recreate
            # a service whenever an optional provider (matter-server's border router) comes or goes
            services[name] = {"restart": "unless-stopped", **_render(self.services[name].compose, values)}
        compose = json.dumps({"name": "majordom", "services": services}, indent=2)
        if compose == self._compose:
            return False
        (self.data / "compose.json").write_text(compose)
        if not self.docker:
            self._compose = compose
            return True
        args = ["up", "-d", "--remove-orphans"] if services else ["down"]  # integrations retry while services start
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(self.data / "compose.json"), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        output = (await process.communicate())[0].decode(errors="replace")
        if process.returncode:
            log.error(f"docker compose {args[0]} failed:\n{output[-3000:]}")
        else:
            self._compose = compose  # only what actually runs; a failure is retried on the next refresh
        return True

    def _save(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "state.json").write_text(json.dumps(self.state, indent=2))

    def _spawn(self, awaitable: Awaitable[None]):
        task = asyncio.ensure_future(awaitable)
        self._notifying.add(task)
        task.add_done_callback(self._notifying.discard)


def _hint(offered_by: Mapping[tuple[str, str], Endpoint], taker: str, family: str) -> str:
    """What to need instead when what took the endpoint offers the same family itself: the router for raw BLE."""
    if endpoint := offered_by.get((taker, family)):
        return f' — use Need("{family}", ["{endpoint.dialect}"])'
    return ""


def _satisfied(needs: Sequence[Need], families: set[str]) -> bool:
    return all(need.family in families for need in needs if need.required)


def fw_endpoints(driver, firmware: str | None, path: str, params: Mapping[str, Any] = {}) -> list[Endpoint]:
    endpoints = driver.firmwares.get(firmware, []) if driver else []
    return [replace(e, url=e.url.format(device=path), params={**e.params, **params}) for e in endpoints]


def _nodes(paths: Mapping[str, str]) -> dict[str, str]:
    """The device node behind each path. After a USB reset a stick keeps its by-id name but may come back
    on another tty; a container keeps the node it was created with, which may then be another device."""
    return {radio_id: os.path.realpath(path) for radio_id, path in paths.items()}


def _usb_id(path: str) -> tuple[str, str] | None:
    """USB vendor and product id of the device behind a serial port; None for a UART."""
    device = Path("/sys/class/tty", Path(os.path.realpath(path)).name, "device").resolve()
    for parent in (device, *device.parents):
        with contextlib.suppress(OSError):
            return (parent / "idVendor").read_text().strip(), (parent / "idProduct").read_text().strip()
    return None


def _holder(path: str, open_it: bool = False) -> str | None:
    """The other program that has a serial port open: one whose open files we may read, or with ``open_it``
    (it opens the port) one that locked it, whoever runs it. Our own containers see the port under another
    name (/dev/ttyRCP), so they don't count."""
    names = {path, os.path.realpath(path)}
    for fd in glob("/proc/[0-9]*/fd/*"):
        with contextlib.suppress(OSError):
            if (pid := fd.split("/")[2]) != str(os.getpid()) and os.readlink(fd) in names:
                return Path(f"/proc/{pid}/comm").read_text().strip()
    if not open_it:
        return None
    try:
        port = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        return "another program" if error.errno == errno.EBUSY else None  # EBUSY: opened exclusively (TIOCEXCL)
    try:
        fcntl.flock(port, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyserial's and node-serialport's exclusive lock
    except OSError:
        return "another program"
    finally:
        os.close(port)
    return None


def _listening(port: int) -> bool:
    """Whether something on the host listens on a TCP port (all services run on the host network)."""
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:  # also one on loopback only, which BSDs let a bind pass
            return True
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a closed connection in TIME_WAIT is no one's
        try:
            probe.bind(("", port))
        except OSError:
            return True
    return False


def _host_endpoints() -> list[Endpoint]:
    """IP, and every Bluetooth adapter as its own single-use ``ble/bluez`` endpoint (the router takes them all, unless
    one is reserved); off Linux, bleak's default adapter on the OS's own stack."""
    adapters = sorted(Path(p).name for p in glob("/sys/class/bluetooth/hci*") if ":" not in Path(p).name)
    if sys.platform != "linux":
        adapters = [""]
    return [Endpoint("ip", shared=True), *(Endpoint("ble", "bluez", name) for name in adapters)]


class _Peripherals(dict):
    """Template access ``{peripherals.thread.url}``; an unbound optional need, or a param it lacks, renders empty. A
    ``many`` need renders its first endpoint."""

    def __getattr__(self, family: str) -> Endpoint:
        bound: Bound = self.get(family, Endpoint(""))
        endpoint = (bound or [Endpoint("")])[0] if isinstance(bound, list) else bound
        return replace(endpoint, params=_Params(endpoint.params))


class _Params(dict):
    def __missing__(self, name: str) -> None:
        return None


class _Host:
    uid, gid = os.getuid(), os.getgid()  # for services that should write their data as the hub's user

    @property
    def interface(self) -> str:
        """The backbone interface: a wired one with a link (Wi-Fi drops the multicast a Thread border
        router relies on), else the one of the default route."""
        for wired in sorted(glob("/sys/class/net/e*/carrier")):
            with contextlib.suppress(OSError):
                if Path(wired).read_text().strip() == "1":
                    return Path(wired).parent.name
        with contextlib.suppress(OSError):
            for line in Path("/proc/net/route").read_text().splitlines()[1:]:
                name, destination, *_ = line.split()
                if destination == "00000000":
                    return name
        return "eth0"


class _Formatter(string.Formatter):
    """None renders empty; a flag with a format spec renders the spec when set: ``{x:&uart-flow-control}``."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, bool) and format_spec:
            return format_spec if value else ""
        return "" if value is None else super().format_field(value, format_spec)


def _render(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _Formatter().vformat(value, (), values)
    if isinstance(value, Mapping):
        return {k: _render(v, values) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_render(v, values) for v in value]
    return value


__all__ = ["Runtime", "Radio", "PlatformSettings", "Consumer"]
