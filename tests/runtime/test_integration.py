"""An integration controller run by the runtime like any peripheral: its platform, settings and peripherals."""

import json
from uuid import UUID

from pydantic import BaseModel
from support import run, settle, state, until

from majordom_integration_sdk import AbstractController, Endpoint, Need, Service
from majordom_integration_sdk.runtime import HostNetwork, PeripheralState, PeripheralStatus
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter
from majordom_integration_sdk.testing import build_test_platform


class Greeting(BaseModel):
    greeting: str = "hello"


def echo_server() -> Service:
    return Service(
        "echo-server",
        compose={"image": "echo", "command": ["--greeting", "{settings.greeting}"]},
        needs=[Need("ip")],
        provides=[Endpoint("example", "echo", "ws://127.0.0.1:{ports.api}")],
        settings=Greeting,
        ports={"api": 5600},
    )


class EchoController(AbstractController[Device, Parameter]):
    needs = [Need("example", ["echo"])]

    class Settings(BaseModel):
        is_loud: bool = False

    settings: Settings

    def __init__(self, platform, peripherals=None):
        super().__init__(platform, peripherals)
        self.server = self.peripherals[0]
        self.started_with: list[tuple[str, bool]] = []

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        self.started_with.append((self.peripherals.endpoint("example").url, self.settings.is_loud))

    async def stop(self):
        pass

    async def settings_did_change(self, before) -> bool:
        await self.peripherals.restart(self.server)  # it reconfigures the server it brought along
        return True

    async def pair_device(self, discovery, credentials):
        pass

    async def unpair(self, device):
        pass

    async def identify(self, device):
        pass

    async def fetch(self, device):
        pass

    async def send_command(self, command, device, parameter):
        pass


def controller() -> EchoController:
    EchoController.services = [echo_server()]
    return EchoController(build_test_platform())


async def test_an_integration_runs_once_the_server_it_brings_along_is_ready(tmp_path):
    echo = controller()
    runtime = await run(tmp_path, HostNetwork(), echo, is_docker_enabled=False)

    assert runtime.peripherals == [runtime.peripherals[0], echo, echo.server]
    assert runtime.status(echo) == PeripheralStatus(PeripheralState.RUNNING)
    assert echo.started_with == [("ws://127.0.0.1:5600", False)]  # its needs bound, its settings given, before start
    assert runtime.users(echo.server) == ["Echo"]


async def test_an_integration_keeps_its_settings_and_restarts_the_server_it_brought(tmp_path):
    echo = controller()
    runtime = await run(tmp_path, HostNetwork(), echo, is_docker_enabled=False)
    compose = tmp_path / "data" / "compose.json"

    await runtime.update_settings(echo.server, {"greeting": "hi"})
    await settle(runtime)
    assert json.loads(compose.read_text())["services"]["echo-server"]["command"] == ["--greeting", "hi"]

    await runtime.update_settings(echo, {"is_loud": True})
    await until(runtime, lambda: state(runtime, echo.server) == PeripheralState.RUNNING)
    assert echo.settings.is_loud and len(echo.started_with) == 1  # applied in place: only its server restarted

    again = controller()  # after a reboot
    await run(tmp_path, HostNetwork(), again, is_docker_enabled=False)
    assert again.started_with == [("ws://127.0.0.1:5600", True)]


async def test_an_integration_stops_with_a_reason_when_its_server_cant_run(tmp_path):
    echo = controller()
    runtime = await run(tmp_path, echo, is_docker_enabled=False)  # no network for the server
    assert runtime.status(echo) == PeripheralStatus(
        PeripheralState.WAITING, "need example/echo: echo-server can't run (need ip/*: nothing offers it)"
    )
    assert echo.started_with == []
