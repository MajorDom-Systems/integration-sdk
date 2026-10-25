"""An integration controller run by the runtime like any peripheral: its platform, settings and peripherals."""

import json
from uuid import UUID

from pydantic import BaseModel
from support import run, settle

from majordom_integration_sdk import AbstractController, Endpoint, Need, Peripheral, Platform, Service
from majordom_integration_sdk.runtime import HostNetwork, PeripheralState, PeripheralStatus
from majordom_integration_sdk.runtime import StatusKind as K
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter
from majordom_integration_sdk.testing import FakeCompose, build_test_platform


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

    def __init__(self, platform: Platform, *, settings: Settings | None = None):
        super().__init__(platform, settings=settings)
        self.started_with: list[tuple[str, bool]] = []

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        self.started_with.append((self.peripherals.endpoint("example").url, self.settings.is_loud))

    async def stop(self):
        pass

    async def settings_did_change(self, before) -> bool:
        return True  # it tells the server over its connection

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


def peripherals(platform: Platform) -> list[Peripheral]:
    """What the integration package exports: its server, and the controller that needs it."""
    return [echo_server(), EchoController(platform)]


def integration() -> tuple[Service, EchoController]:
    """What the integration exports, given its platform as by the Hub."""
    server, echo = peripherals(build_test_platform())
    assert isinstance(server, Service) and isinstance(echo, EchoController)
    return server, echo


async def test_an_integration_runs_once_the_server_it_provides_serves(tmp_path):
    server, echo = integration()
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo)

    assert runtime.status(echo) == PeripheralStatus(PeripheralState.RUNNING)
    assert echo.started_with == [("ws://127.0.0.1:5600", False)]  # its needs bound, its settings given, before start
    assert runtime.users(server) == ["echo"] and runtime.name(echo) == "echo"  # known by its slug


async def test_an_integration_and_its_server_keep_their_settings(tmp_path):
    server, echo = integration()
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo)
    compose = tmp_path / "data" / "peripherals" / "compose" / "compose.json"

    await runtime.update_settings(server, {"greeting": "hi"})
    await settle(runtime)
    assert json.loads(compose.read_text())["services"]["echo-server"]["command"] == ["--greeting", "hi"]
    assert echo.started_with == [("ws://127.0.0.1:5600", False)]  # the server restarted under it: it reconnects

    await runtime.update_settings(echo, {"is_loud": True})
    await settle(runtime)
    assert echo.settings.is_loud and len(echo.started_with) == 1  # applied in place

    server, again = integration()  # after a reboot
    await run(tmp_path, HostNetwork(), FakeCompose(), server, again)
    assert again.started_with == [("ws://127.0.0.1:5600", True)]


async def test_an_integration_waits_with_a_reason_when_its_server_cant_run(tmp_path):
    server, echo = integration()
    runtime = await run(tmp_path, FakeCompose(), server, echo)  # no network for the server
    assert runtime.status(echo) == PeripheralStatus(
        PeripheralState.WAITING,
        K.NEEDS_UNBOUND,
        "need example/echo: echo-server can't run (need ip/*: nothing offers it)",
    )
    assert echo.started_with == []


async def test_a_controller_given_its_settings_keeps_them_until_the_user_changes_them(tmp_path):
    echo = EchoController(build_test_platform(), settings=EchoController.Settings(is_loud=True))  # library mode
    await run(tmp_path, HostNetwork(), FakeCompose(), echo_server(), echo)
    assert echo.started_with == [("ws://127.0.0.1:5600", True)]

    server, again = integration()  # after a reboot
    await run(tmp_path, HostNetwork(), FakeCompose(), server, again)
    assert again.started_with == [("ws://127.0.0.1:5600", True)]
