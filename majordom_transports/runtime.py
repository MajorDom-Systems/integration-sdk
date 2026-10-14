"""Find radios, bind every consumer's needs to what radios, services and the host offer, and run the
services that end up bound through Docker Compose — then keep it in sync as sticks come and go."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import string
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Any, Literal, Protocol

from majordom_integration_sdk import Context, Endpoint, Need, Service, WifiCredentials
from pydantic import BaseModel, Field

from majordom_transports.drivers import DRIVERS, RADIO_USB_IDS
from majordom_transports.services import SERVICES

log = logging.getLogger(__name__)

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


class HubSettings(BaseModel):
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
    ):
        """
        consumers: integration controller classes by slug.
        docker: run the bound services; False leaves them to you (pin their endpoints).
        scan: look for USB sticks in /dev/serial/by-id; `devices` adds paths by id.
        board: soldered radios, ``{"id", "path", "driver", "firmware"}``, used without asking.
        pinned: endpoints you provide yourself by family; they win over anything found.
        """
        self.data, self.consumers, self.docker, self.scan = data, dict(consumers), docker, scan
        self.devices, self.board, self.pinned = dict(devices), list(board), dict(pinned)
        self.default_wifi, self.on_change, self.poll = default_wifi, on_change, poll
        self.services = {s.name: s for c in self.consumers.values() for s in c.services} | {s.name: s for s in SERVICES}
        self.state: dict[str, Any] = {"radios": {}, "pins": {}, "settings": {}}
        with contextlib.suppress(FileNotFoundError):
            self.state |= json.loads((data / "state.json").read_text())
        for radio in self.board:
            self.state["radios"].setdefault(radio["id"], dict(radio))
        self._links: dict[str, dict[str, Endpoint]] = {}  # consumer or service -> family -> endpoint
        self._providers: dict[str, dict[str, str]] = {}  # consumer or service -> family -> provider
        self._present: dict[str, str] = {}
        self._nodes: dict[str, str] = {}  # radio id -> device node its path resolves to (a ttyUSB*)
        self._busy: set[str] = set()  # radios being flashed
        self._compose = ""
        self._learned: dict[tuple[str, str], Mapping[str, Any]] = {}  # bound (provider, url) -> context facts
        self._tasks: list[asyncio.Task] = []  # services setting up and learning
        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task | None = None

    # Lifecycle

    async def start(self):
        await self.refresh()
        self._watcher = asyncio.create_task(self._watch(), name="transports.watch")

    async def stop(self):
        for task in (self._watcher, *self._tasks):
            if task:
                task.cancel()

    async def refresh(self):
        async with self._lock:
            self._present = self._find()
            self._nodes = _nodes(self._present)
            for radio_id, path in self._present.items():
                if radio_id not in self.state["radios"]:
                    self.state["radios"][radio_id] = await self._identify(path) if self._trusted(radio_id, path) else {}
                self.state["radios"][radio_id]["path"] = path
            self._save()
            self._resolve()
            recreated = await self._apply()
            self._follow(recreated)
        if self.on_change:
            await self.on_change()

    def _follow(self, recreated: bool):
        """Have services set up what runs and learn facts from what is bound; known facts stay until read
        again, and go with their provider."""
        for task in self._tasks:
            task.cancel()
        sources = self._sources()
        self._learned = {key: facts for key, facts in self._learned.items() if key in sources}
        self._tasks = []
        for key, (service, endpoint) in sources.items():
            if recreated or key not in self._learned:
                self._tasks.append(asyncio.create_task(self._learn(key, service, endpoint)))
                if key[0] == service.name:  # never someone else's (pinned)
                    settings = self.settings(service.name)
                    self._tasks.append(asyncio.create_task(self._provision(service, endpoint, settings)))

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
            elif found != self._nodes:
                try:
                    await self.refresh()
                except Exception:
                    log.exception("Refreshing radios failed")

    # What the hub (or you) reads

    def links(self, consumer: str) -> dict[str, Endpoint] | None:
        """Bound endpoints of a consumer by family; None while a required need can't be met."""
        return self._links.get(consumer)

    def providers(self, owner: str) -> dict[str, str]:
        """Who serves each bound need of a consumer or service: a radio id, a service name, "host" or "pinned"."""
        return dict(self._providers.get(owner, {}))

    def context(self) -> Context:
        hub = self.settings("hub")
        wifi = next((w for w in hub.wifi if w.ssid == hub.wifi_ssid), None) or self.default_wifi()
        return Context(region=hub.region, wifi=wifi, **self._facts())

    def radios(self) -> list[Radio]:
        result = []
        for radio_id, entry in self.state["radios"].items():
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
                )
            )
        return result

    def settings(self, scope: str) -> Any:
        """Validated settings of a consumer (slug), a service (name) or the hub ("hub"); None if it has none."""
        owner = self.consumers.get(scope) or self.services.get(scope)
        model = HubSettings if scope == "hub" else getattr(owner, "settings", None)
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

    async def set_firmware(self, radio_id: str, firmware: str):
        entry = self.state["radios"][radio_id]
        self._busy.add(radio_id)
        try:
            await self.refresh()  # releases the stick: whatever used it is stopped or unlinked
            channel = self.settings("hub").firmware
            await DRIVERS[entry["driver"]].flash(entry["path"], firmware, channel, self.data / "firmware")
            entry["firmware"] = await DRIVERS[entry["driver"]].identify(entry["path"])
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
                self.state["radios"][radio_id] = await self._identify(self._present[radio_id])
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

    # Internals

    def _find(self) -> dict[str, str]:
        found = {Path(p).name: p for p in glob("/dev/serial/by-id/*")} if self.scan else {}
        found |= {r["id"]: r["path"] for r in self.board if Path(r["path"]).exists()}
        return found | {i: p for i, p in self.devices.items() if Path(p).exists()}

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
        if radio_id not in self._present or radio_id in self._busy:
            return []
        node = self._nodes.get(radio_id, entry["path"])
        if "family" in entry:
            return [Endpoint(entry["family"], entry["dialect"], node)]
        return fw_endpoints(DRIVERS.get(entry.get("driver", "")), entry.get("firmware"), node)

    def _offers(self) -> list[tuple[str, Endpoint]]:
        offers = [("pinned", e) for e in self.pinned.values()] + [("host", e) for e in _host_endpoints()]
        offers += [(i, e) for i, entry in self.state["radios"].items() for e in self._radio_endpoints(i, entry)]
        offers += [(name, e) for name, s in self.services.items() for e in s.provides]
        pins = self.state["pins"]
        return sorted(offers, key=lambda offer: (offer[0] != "pinned", pins.get(offer[1].family) != offer[0]))

    def _resolve(self):
        links: dict[str, dict[str, Endpoint] | None] = {}
        providers: dict[str, dict[str, str]] = {}
        taken: set[tuple[str, str]] = set()
        offers = self._offers()

        def bind(owner: str, needs: Sequence[Need]) -> dict[str, Endpoint] | None:
            if owner in links:
                return links[owner]
            links[owner] = None  # also guards against cycles
            bound, by = {}, {}
            for need in needs:
                for provider, endpoint in offers:
                    if not need.accepts(endpoint) or (provider, endpoint.url) in taken or provider == owner:
                        continue
                    if provider in self.services and bind(provider, self.services[provider].needs) is None:
                        continue
                    if not endpoint.shared:
                        taken.add((provider, endpoint.url))
                    bound[need.family], by[need.family] = endpoint, provider
                    break
                else:
                    if need.required:
                        return None
            links[owner], providers[owner] = bound, by
            return bound

        for name, consumer in self.consumers.items():
            bind(name, consumer.needs)
        self._links = {o: v for o, v in links.items() if v is not None}
        self._providers = providers

    def _roots(self, owner: str) -> set[str]:
        """Every provider ``owner`` depends on, directly or through services."""
        direct = set(self._providers.get(owner, {}).values())
        return direct.union(*(self._roots(p) for p in direct if p in self.services))

    def _sources(self) -> dict[tuple[str, str], tuple[Service, Endpoint]]:
        """Bound endpoints by (provider, url) with the service that sets them up or reads them: their own,
        or for a pinned one any service providing that kind."""
        readers = {(e.family, e.dialect): s for s in self.services.values() for e in s.provides}
        sources = {}
        for owner, providers in self._providers.items():
            for family, provider in providers.items():
                endpoint = self._links.get(owner, {}).get(family)
                kind = readers.get((endpoint.family, endpoint.dialect)) if endpoint and provider == "pinned" else None
                if endpoint and (service := self.services.get(provider) or kind):
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
        for name in self._links.keys() & self.services.keys():
            data = self.data / "services" / name
            data.mkdir(parents=True, exist_ok=True)
            values = {"data": data, "links": _Links(self._links[name]), "settings": self.settings(name)}
            values |= {"context": self.context(), "host": _Host()}
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


def _satisfied(needs: Sequence[Need], families: set[str]) -> bool:
    return all(need.family in families for need in needs if need.required)


def fw_endpoints(driver, firmware: str | None, path: str) -> list[Endpoint]:
    endpoints = driver.firmwares.get(firmware, []) if driver else []
    return [Endpoint(e.family, e.dialect, e.url.format(device=path), e.shared) for e in endpoints]


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


def _host_endpoints() -> list[Endpoint]:
    ble = [Endpoint("ble", "bluez", Path(p).name, shared=True) for p in glob("/sys/class/bluetooth/hci*")[:1]]
    return [Endpoint("ip", shared=True), *ble]


class _Links(dict):
    """Template access ``{links.thread.url}``; an unbound optional link renders empty."""

    def __getattr__(self, family: str) -> Endpoint:
        return self.get(family, Endpoint(""))


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
    def format_field(self, value: Any, format_spec: str) -> str:
        return "" if value is None else super().format_field(value, format_spec)


def _render(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _Formatter().vformat(value, (), values)
    if isinstance(value, Mapping):
        return {k: _render(v, values) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_render(v, values) for v in value]
    return value


__all__ = ["Runtime", "Radio", "HubSettings", "Consumer"]
