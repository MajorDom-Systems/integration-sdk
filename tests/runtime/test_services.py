"""Containers as peripherals: what they render, the ports they get, when they are ready, what they learn."""

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel
from support import run, settle, state, until

from majordom_integration_sdk import Context, Endpoint, Need, Service
from majordom_integration_sdk.runtime import PeripheralState, PeripheralStatus
from majordom_integration_sdk.runtime.peripherals.services import builtin
from majordom_integration_sdk.runtime.peripherals.services.compose import Compose
from majordom_integration_sdk.testing import FakePeripheral, RecordingRuntimeOutput

IP = Endpoint("ip", is_shared=True)
MULTIPAN = Endpoint("multipan", "silabs-cpc", "/dev/ttyUSB0", params={"baud": 460800, "flow_control": True})
NCP = Endpoint("zigbee", "ezsp", "/dev/ttyUSB1", params={"baud": 115200, "flow_control": True})
RCP = Endpoint("thread", "spinel", "/dev/ttyUSB2", params={"baud": 460800, "flow_control": True})


@pytest.fixture(autouse=True)
def listening(monkeypatch) -> set[int]:
    """Host ports something listens on; none unless a test says so."""
    ports: set[int] = set()
    monkeypatch.setattr("majordom_integration_sdk.runtime.peripherals.services.compose._listening", ports.__contains__)
    return ports


@pytest.fixture(autouse=True)
def no_thread_network(monkeypatch):
    """The built-in border routers form and read no real network."""

    async def nothing(*args: Any):
        return None

    monkeypatch.setattr(builtin, "form_network", nothing)
    monkeypatch.setattr(builtin, "active_dataset", nothing)


def host() -> FakePeripheral:
    return FakePeripheral("host", provides=[IP])


def stick(name: str, *provides: Endpoint) -> FakePeripheral:
    return FakePeripheral(name, provides=provides)


def zigbee() -> FakePeripheral:
    return FakePeripheral("zigbee", [Need("zigbee")])


def matter(**compose: Any) -> tuple[FakePeripheral, Service]:
    server = Service(
        "matterjs-server",
        compose={"image": "matter", "environment": {"OTBR": "{peripherals.thread.url}"} | compose},
        needs=[Need("ip"), Need("thread", ["otbr-rest"], is_required=False)],
        provides=[Endpoint("matter", "matter-server-ws", "ws://127.0.0.1:{ports.api}/ws")],
        ports={"api": 5580},
    )
    return FakePeripheral("matter", [Need("matter", ["matter-server-ws"])], peripherals=[server]), server


def compose(tmp_path) -> dict[str, Any]:
    return json.loads((tmp_path / "data" / "compose.json").read_text())["services"]


async def test_a_multipan_stick_serves_zigbee_and_matter_through_one_container(tmp_path):
    radio, integration, (app, _) = stick("sky", MULTIPAN), zigbee(), matter()
    peripherals = (host(), radio, builtin.silabs_multiprotocol, builtin.otbr, integration, app)
    runtime = await run(tmp_path, *peripherals, is_docker_enabled=False)

    assert integration.peripherals.endpoint("zigbee") == Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")
    assert app.peripherals.endpoint("matter").url == "ws://127.0.0.1:5580/ws"
    services = compose(tmp_path)
    assert set(services) == {"silabs-multiprotocol", "matterjs-server"}  # no one binds the standalone border router
    assert services["silabs-multiprotocol"]["devices"][0] == "/dev/ttyUSB0:/dev/ttyRCP"
    environment = services["silabs-multiprotocol"]["environment"]
    assert (environment["RADIO_BAUD"], environment["RADIO_FLOW_CONTROL"]) == ("460800", "True")
    assert services["matterjs-server"]["environment"]["OTBR"] == "http://127.0.0.1:8081"
    assert "depends_on" not in services["matterjs-server"]  # its border router coming and going doesn't restart it
    assert runtime.users(radio) == ["silabs-multiprotocol", "zigbee", "matterjs-server", "matter"]
    assert runtime.uses(app) == ["matterjs-server", "host", "silabs-multiprotocol", "sky"]
    assert runtime.reach([MULTIPAN]) == ["thread", "zigbee"]  # through the containers it would run
    assert runtime.status(builtin.otbr) == PeripheralStatus(
        PeripheralState.WAITING, "need thread/spinel: nothing offers it"
    )


async def test_split_radios_use_direct_zigbee_and_a_standalone_border_router(tmp_path):
    ncp, rcp, integration, (app, _) = stick("ncp", NCP), stick("rcp", RCP), zigbee(), matter()
    peripherals = (host(), ncp, rcp, builtin.silabs_multiprotocol, builtin.otbr, integration, app)
    await run(tmp_path, *peripherals, is_docker_enabled=False)

    assert integration.peripherals.endpoint("zigbee") == NCP
    assert set(compose(tmp_path)) == {"otbr", "matterjs-server"}
    assert compose(tmp_path)["otbr"]["devices"][0] == "/dev/ttyUSB2:/dev/ttyRCP"
    rcp_url = "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate=460800&uart-flow-control"
    assert compose(tmp_path)["otbr"]["environment"]["OT_RCP_DEVICE"] == rcp_url


async def test_without_radios_zigbee_waits_and_matter_runs_over_ip(tmp_path):
    integration, (app, _) = zigbee(), matter()
    peripherals = (host(), builtin.silabs_multiprotocol, builtin.otbr, integration, app)
    runtime = await run(tmp_path, *peripherals, is_docker_enabled=False)

    assert state(runtime, integration) == PeripheralState.WAITING
    assert state(runtime, app) == PeripheralState.RUNNING
    assert compose(tmp_path)["matterjs-server"]["environment"]["OTBR"] == ""


async def test_an_endpoint_given_first_replaces_the_border_router(tmp_path):
    mine = stick("pinned", Endpoint("thread", "otbr-rest", "http://mine:8081", is_shared=True))
    app, _ = matter()
    await run(tmp_path, mine, host(), stick("rcp", RCP), builtin.otbr, app, is_docker_enabled=False)

    assert set(compose(tmp_path)) == {"matterjs-server"}
    assert compose(tmp_path)["matterjs-server"]["environment"]["OTBR"] == "http://mine:8081"


async def test_a_disabled_integration_takes_its_container_down(tmp_path):
    app, server = matter()
    runtime = await run(tmp_path, host(), app, is_docker_enabled=False)
    assert set(compose(tmp_path)) == {"matterjs-server"}

    await runtime.disable(app)
    assert compose(tmp_path) == {}


class Keys(BaseModel):
    key: str = "secret"


async def test_a_service_setting_recreates_only_that_service(tmp_path):
    zwave_server = Service(
        "zwave-js-server",
        compose={"image": "zwave", "environment": {"KEY": "{settings.key}"}},
        needs=[Need("ip")],
        provides=[Endpoint("zwave", "zwave-js-ws", "ws://127.0.0.1:3000")],
        settings=Keys,
    )
    zwave = FakePeripheral("zwave", [Need("zwave")], peripherals=[zwave_server])
    app, _ = matter()
    runtime = await run(tmp_path, host(), zwave, app, is_docker_enabled=False)
    before = compose(tmp_path)
    assert before["zwave-js-server"]["environment"]["KEY"] == "secret"

    await runtime.update_settings(zwave_server, {"key": "other"})
    await settle(runtime)
    after = compose(tmp_path)
    assert {name for name in before if before[name] != after[name]} == {"zwave-js-server"}
    assert zwave.calls == ["start"]  # what it is bound to is the same: it reconnects on its own


async def test_the_context_renders_and_a_change_recreates_only_what_renders_it(tmp_path):
    region = Context(region="Europe")
    app, server = matter(REGION="{context.region}")
    plain = Service("plain", {"image": "plain"}, [Need("ip")], [Endpoint("plain")])
    user = FakePeripheral("user", [Need("plain")], peripherals=[plain])
    runtime = await run(tmp_path, host(), app, user, context=lambda: region, is_docker_enabled=False)
    assert compose(tmp_path)["matterjs-server"]["environment"]["REGION"] == "Europe"

    before, region = region, Context(region="USA")
    await runtime.context_did_change(before)
    await settle(runtime)
    assert compose(tmp_path)["matterjs-server"]["environment"]["REGION"] == "USA"
    assert runtime.status(server).state == PeripheralState.RUNNING
    assert app.changes == user.changes == [("context", before)]  # the integrations hear it too


async def test_services_get_free_host_ports_and_keep_them(tmp_path, listening):
    async def ezsp() -> tuple[str, str]:
        """The zigbee URL bound after a (re)boot, and the port the container got."""
        integration = zigbee()
        peripherals = (host(), stick("sky", MULTIPAN), builtin.silabs_multiprotocol, integration)
        runtime = await run(tmp_path, *peripherals, is_docker_enabled=False)
        port = compose(tmp_path)["silabs-multiprotocol"]["environment"]["EZSP_TCP_PORT"]
        await runtime.stop()
        return integration.peripherals.endpoint("zigbee").url, port

    assert await ezsp() == ("socket://127.0.0.1:9999", "9999")  # the preferred port, free

    listening.add(9999)  # after a reboot something else took it before our container came up
    moved, port = await ezsp()
    assert moved == f"socket://127.0.0.1:{port}" != "socket://127.0.0.1:9999"

    listening.clear()  # the preferred port is free again: the service stays where it is now
    assert (await ezsp())[0] == moved


async def test_a_port_held_by_our_own_running_container_is_kept(tmp_path, listening, monkeypatch):
    peripherals = (host(), stick("sky", MULTIPAN), builtin.silabs_multiprotocol)
    runtime = await run(tmp_path, *peripherals, zigbee(), is_docker_enabled=False)
    await runtime.stop()
    listening.update({9999, 8081})  # our silabs-multiprotocol container is up and holds them

    async def containers(self) -> set[str]:
        return {"silabs-multiprotocol"}

    monkeypatch.setattr(Compose, "_containers", containers)
    integration = zigbee()
    await run(tmp_path, *peripherals, integration, is_docker_enabled=False)
    assert integration.peripherals.endpoint("zigbee").url == "socket://127.0.0.1:9999"


async def test_services_preferring_the_same_port_get_different_ones(tmp_path):
    bridge = Service(
        "bridge",
        {"image": "bridge", "command": ["--port", "{ports.api}"]},
        provides=[Endpoint("bridge", "x", "ws://127.0.0.1:{ports.api}")],
        ports={"api": 5580},
    )
    user = FakePeripheral("user", [Need("bridge")], peripherals=[bridge])
    app, _ = matter()
    await run(tmp_path, host(), app, user, is_docker_enabled=False)
    api = compose(tmp_path)["bridge"]["command"][1]
    assert user.peripherals.endpoint("bridge").url == f"ws://127.0.0.1:{api}"
    ports = {api, app.peripherals.endpoint("matter").url.rsplit(":", 1)[1].removesuffix("/ws")}
    assert len(ports) == 2 and "5580" in ports  # one got the port both prefer, the other another


class Router(Service):
    """A border router that is set up once it answers and knows its Thread network."""

    def __init__(self):
        super().__init__("router", {"image": "router"}, [Need("ip")], [Endpoint("thread", "rest", "http://ours")])
        self.release, self.calls = asyncio.Event(), []

    async def provision(self, endpoint: Endpoint):
        await self.release.wait()
        self.calls.append(("provision", endpoint.url))

    async def learn(self, endpoint: Endpoint):
        self.calls.append(("learn", endpoint.url))
        return {"thread_dataset": "0e08"}


async def test_what_needs_a_service_waits_for_its_setup_then_hears_what_it_learned(tmp_path):
    router, output = Router(), RecordingRuntimeOutput()
    thread = FakePeripheral("thread", [Need("thread")], peripherals=[router])
    runtime = await run(tmp_path, host(), thread, output=output, is_docker_enabled=False)
    assert runtime.status(router) == PeripheralStatus(PeripheralState.RUNNING, is_ready=False)
    assert runtime.status(thread) == PeripheralStatus(PeripheralState.WAITING, "waiting for router")

    await runtime.refresh()  # something else changed meanwhile: its setup goes on
    router.release.set()
    await until(runtime, lambda: state(runtime, thread) == PeripheralState.RUNNING)
    assert router.calls == [("provision", "http://ours"), ("learn", "http://ours")]
    assert output.learned == [("router", {"thread_dataset": "0e08"})]

    await thread.peripherals.restart(router)  # recreated: it may hold another network now
    await until(runtime, lambda: len(output.learned) == 2)
    assert router.calls[-2:] == [("provision", "http://ours"), ("learn", "http://ours")]
    assert thread.calls == ["start"]


class Docker:
    """Docker Compose, faked: ``calls`` it got; a container runs once applied, and its ports answer while
    ``is_answering``. Each call fails while ``failure`` is set."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.composes: list[Compose] = []
        self.is_answering = True
        self.failure = b""

    async def exec(self, *args: str, **kwargs: Any) -> "Docker":
        self.calls.append(args)
        return self

    @property
    def returncode(self) -> int:
        return 1 if self.failure else 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.failure, b""

    def holds(self, port: int) -> bool:
        running = (
            ports for compose in self.composes for name, ports in compose.ports.items() if name in compose._running
        )
        return self.is_answering and any(port in ports.values() for ports in running)


@pytest.fixture
def docker(monkeypatch, listening) -> Docker:
    docker, init = Docker(), Compose.__init__

    def record(self, *args: Any, **kwargs: Any):
        init(self, *args, **kwargs)
        docker.composes.append(self)

    async def containers(self) -> set[str]:
        return set(self._running)

    monkeypatch.setattr(Compose, "__init__", record)
    monkeypatch.setattr(Compose, "_containers", containers)
    monkeypatch.setattr("asyncio.create_subprocess_exec", docker.exec)
    monkeypatch.setattr(
        "majordom_integration_sdk.runtime.peripherals.services.compose._listening",
        lambda port: port in listening or docker.holds(port),
    )
    return docker


async def test_a_failed_compose_up_is_tried_again(tmp_path, docker):
    docker.failure = b"Conflict. The container name is already in use"
    app, server = matter()
    runtime = await run(tmp_path, host(), app)
    assert runtime.status(server).state == PeripheralState.WAITING
    assert "Conflict. The container name is already in use" in runtime.status(server).reason

    docker.failure = b""
    await until(runtime, lambda: state(runtime, app) == PeripheralState.RUNNING)
    assert [call[-3:] for call in docker.calls] == [("up", "-d", "--remove-orphans")] * 2


async def test_a_container_is_ready_once_compose_runs_it_and_its_ports_answer(tmp_path, docker):
    docker.is_answering = False
    app, server = matter()
    runtime = await run(tmp_path, host(), app)
    assert runtime.status(server) == PeripheralStatus(PeripheralState.RUNNING, is_ready=False)
    assert runtime.status(app).reason == "waiting for matterjs-server"

    docker.is_answering = True
    await until(runtime, lambda: state(runtime, app) == PeripheralState.RUNNING)


async def test_restarting_a_service_recreates_only_its_container(tmp_path, docker):
    app, server = matter()
    integration = zigbee()
    peripherals = (host(), stick("sky", MULTIPAN), builtin.silabs_multiprotocol, integration, app)
    runtime = await run(tmp_path, *peripherals)
    await until(runtime, lambda: state(runtime, app) == state(runtime, integration) == PeripheralState.RUNNING)
    before = compose(tmp_path)

    await app.peripherals.restart(server)
    await until(runtime, lambda: runtime.status(server).is_ready)
    assert compose(tmp_path) == before
    assert [call[-1] for call in docker.calls][-2:] == ["--remove-orphans"] * 2  # without it, then with it again
    assert (app.calls, integration.calls) == (["start"], ["start"])
