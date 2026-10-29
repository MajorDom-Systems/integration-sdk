"""Docker Compose as the container host of every `Service`: the containers of the ones that run, as one project."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import socket
import string
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from functools import partial
from glob import glob
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from majordom_integration_sdk.files import read_json, write_json
from majordom_integration_sdk.peripherals import Bound, Endpoint, Peripheral, Service

log = logging.getLogger(__name__)

PORTS = range(45800, 46000)  # where a service goes when the port it prefers is taken
READY_POLL = 1  # seconds between checks of whether a container is up, its setup done and it is healthy


class Compose(Peripheral):
    """Docker Compose as the container host every `Service` needs (``container/compose``): the containers of the
    services that run, as one ``majordom`` project in ``compose.json`` of its documents folder, each service with its
    own documents folder as ``{data}`` (created when it renders, the folders under it the service binds when it is
    created: Docker would create them as root). Each gets the host ports it prefers when free, kept across restarts.
    A service's start returns once compose reports its container running, its ports answer, its `Service.provision`
    finished and it is healthy. It runs while a service does; while Docker isn't reachable its start fails, and the
    services wait, saying so.

    ``listen_address`` is where the services on the host network answer, ``{host.listen_address}``: loopback, the hub
    only, by default; ``0.0.0.0`` for a lab harness reached from another host, where nothing guards them."""

    name = "compose"
    is_on_demand = True

    def __init__(self, listen_address: str = "127.0.0.1"):
        self.listen_address = listen_address
        self._running: dict[str, Service] = {}
        self._tentative: dict[str, dict[str, int]] = {}  # the ports a service that doesn't run would get
        self._rendered: dict[str, Mapping[str, Any]] = {}  # what each one runs with, rendered when it was created
        self._applied: Mapping[str, Any] | None = None  # the compose file docker last took
        self._lock = asyncio.Lock()

    @property
    def provides(self) -> list[Endpoint]:
        return [Endpoint("container", "compose", "compose", is_shared=True, instance=self)]

    @functools.cached_property
    def ports(self) -> dict[str, dict[str, int]]:
        """The host ports each service keeps, by its name."""
        return read_json(self.platform.documents_folder(self) / "ports.json")

    async def start(self):
        try:
            await self._docker("info", "--format", "{{.ServerVersion}}")
        except RuntimeError as error:
            raise RuntimeError(f"Docker isn't reachable: {error}") from None

    # ServiceHost

    def provided(self, service: Service) -> list[Endpoint]:
        values = self._values(service)
        ports = values["ports"]
        return [
            replace(e, url=_render(e.url, {"ports": ports}), params=_render(e.params, values))
            for e in service.templates
        ]

    async def up(self, service: Service):
        await self._keep_ports(service)
        self._running[service.name] = service
        self._rendered[service.name] = self._render(service)
        _make_bound_folders(self._rendered[service.name], service.platform.documents_folder(service))
        try:
            await self._apply()
        except Exception:
            self._running.pop(service.name)
            raise
        await _until(lambda: self._is_up(service), f"{service.name} isn't up")
        for endpoint in service.provides:
            await _until(partial(_provision, service, endpoint), f"{service.name} failed to set up {endpoint.url}")
        await _until(service.is_healthy, f"{service.name} isn't healthy")

    async def down(self, service: Service):
        if self._running.pop(service.name, None):
            self._rendered.pop(service.name, None)
            try:
                await self._apply()
            except Exception as error:
                log.error(f"Removing {service.name}'s container failed: {error}")

    def is_current(self, service: Service) -> bool:
        return service.name not in self._running or self._rendered.get(service.name) == self._render(service)

    async def wipe(self, service: Service):
        """Empty the documents folder of ``service``, which doesn't run: through Docker, in its own image (pulled
        already, with the tools in it), as its container wrote there as root, which the hub's user can't remove (the
        Thread network, zigbeed's state). RuntimeError while it runs, or when Docker can't."""
        if service.name in self._running:
            raise RuntimeError(f"{service.name} runs: its container would write on")
        folder = service.platform.documents_folder(service)
        if not (image := service.compose.get("image")) or not any(folder.iterdir()):
            return  # no container of its own, or nothing to remove
        await self._docker(
            *("run", "--rm", "--pull", "never", "--network", "none", "--userns", "host", "--user", "0"),
            *("--entrypoint", "find", "-v", f"{folder}:/d", image, "/d", "-mindepth", "1", "-delete"),
        )

    # Internals

    def _values(self, service: Service) -> dict[str, Any]:
        """What its compose and the params it provides render with."""
        return {
            "data": service.platform.documents_folder(service),
            "peripherals": _Peripherals(service.peripherals.bound),
            "settings": service.settings,
            "platform": service.platform,
            "host": _Host(self.listen_address),
            "ports": SimpleNamespace(**self._ports(service)),
        }

    def _render(self, service: Service) -> Mapping[str, Any]:
        # no depends_on: everything retries until its links answer, and a dependency would recreate
        # a service whenever an optional provider (matter-server's border router) comes or goes
        return {"restart": "unless-stopped", **_render(service.compose, self._values(service))}

    def _ports(self, service: Service) -> dict[str, int]:
        """Its host ports: the ones it kept unless another service took one since, else the preferred one when free.
        One that doesn't run is told what it would get."""
        mine = {**self._tentative.get(service.name, {}), **self.ports.get(service.name, {})}
        others = {port for name, ports in self.ports.items() if name != service.name for port in ports.values()}
        for name, preferred in service.ports.items():
            if not mine.get(name) or mine[name] in others:
                mine[name] = next(p for p in (preferred, *PORTS) if p not in others and not _is_listening(p))
        self._tentative[service.name] = mine
        return mine

    async def _keep_ports(self, service: Service):
        """Keep its ports from now on; move off one something else took meanwhile, e.g. after a reboot before our
        container came up. Not knowing which containers run, it moves nothing: it raises."""
        ports = self._ports(service)
        if any(_is_listening(port) for port in ports.values()) and service.name not in await self._containers():
            self.ports.pop(service.name, None)
            self._tentative[service.name] = {name: port for name, port in ports.items() if not _is_listening(port)}
            ports = self._ports(service)
        self.ports[service.name] = ports
        write_json(self.platform.documents_folder(self) / "ports.json", self.ports)

    async def _apply(self):
        """Have docker take what runs now. Applies run in turn, each to its end even when its caller is cancelled:
        one cut short would leave a container that the next one, finding nothing changed, keeps."""
        await asyncio.shield(asyncio.ensure_future(self._apply_in_turn()))

    async def _apply_in_turn(self):
        async with self._lock:
            compose, path = {"name": "majordom", "services": dict(self._rendered)}, self._path()
            if compose == self._applied:
                return
            write_json(path, compose)
            self._applied = None  # unknown until docker took it all
            args = ["up", "-d", "--remove-orphans"] if self._rendered else ["down"]
            await self._docker("compose", "-f", str(path), *args)
            self._applied = compose  # only what actually runs: a failure is tried again

    def _path(self) -> Path:
        return self.platform.documents_folder(self) / "compose.json"

    async def _is_up(self, service: Service) -> bool:
        ports = self.ports.get(service.name, {}).values()
        return all(_is_listening(port) for port in ports) and service.name in await self._containers()

    async def _containers(self) -> set[str]:
        """Our services whose containers are up."""
        return set((await self._docker("compose", "-p", "majordom", "ps", "--status", "running", "--services")).split())

    async def _docker(self, *args: str) -> str:
        """What ``docker`` with ``args`` printed; RuntimeError when it failed."""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            output = (await process.communicate())[0].decode(errors="replace")
        except OSError as error:  # no docker at all
            raise RuntimeError(str(error)) from error
        if process.returncode:
            raise RuntimeError(f"docker {' '.join(args[:3])} failed:\n{output[-3000:]}")
        return output


async def _until(check: Callable[[], Awaitable[bool]], failure: str):
    """Ask ``check`` until it says yes; one that raises says no."""
    while True:
        try:
            if await check():
                return
        except Exception as error:
            log.warning(f"{failure}, trying again: {error}")
        await asyncio.sleep(READY_POLL)


async def _provision(service: Service, endpoint: Endpoint) -> bool:
    await service.provision(endpoint)
    return True


def _make_bound_folders(compose: Mapping[str, Any], data: Path):
    """Create the folders under ``data`` that ``compose`` binds, as the hub's user: Docker would create them as root."""
    for volume in compose.get("volumes", []):
        source = Path(volume.get("source", "") if isinstance(volume, Mapping) else volume.split(":")[0])
        if source.is_relative_to(data):
            source.mkdir(parents=True, exist_ok=True)


def _is_listening(port: int) -> bool:
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


class _Peripherals(dict):
    """Template access ``{peripherals.thread.url}``; an unbound optional need, or a param it lacks, renders empty. An
    ``is_many`` need renders its first endpoint."""

    def __getattr__(self, family: str) -> Endpoint:
        bound: Bound = self.get(family, Endpoint(""))
        endpoint = bound if isinstance(bound, Endpoint) else (bound or [Endpoint("")])[0]
        return replace(endpoint, params=_Params(endpoint.params))


class _Params(dict):
    def __missing__(self, name: str):
        return None


class _Host:
    uid, gid = os.getuid(), os.getgid()  # for services that should write their data as the hub's user

    def __init__(self, listen_address: str):
        self.listen_address = listen_address

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
    """None renders empty; a flag with a format spec renders the spec when set: ``{x:&uart-flow-control}``; a ``!`` spec
    renders what follows when the value is empty, e.g. a proxy without an adapter: ``{peripherals.ble.url:!true}``, a
    ``?`` spec when it isn't, e.g. a backend only with one: ``{peripherals.ble.url:?dbus}``."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec.startswith("!"):
            return "" if value else format_spec[1:]
        if format_spec.startswith("?"):
            return format_spec[1:] if value else ""
        if isinstance(value, bool) and format_spec:
            return format_spec if value else ""
        return "" if value is None else super().format_field(value, format_spec)


def _render(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _Formatter().vformat(value, (), values)
    if isinstance(value, Mapping):
        return {k: _render(v, values) for k, v in value.items()}
    if isinstance(value, list | tuple):  # an item rendering empty is left out: a volume only with an adapter
        return [item for v in value if (item := _render(v, values)) != ""]
    return value
