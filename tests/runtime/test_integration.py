"""An integration controller run by the runtime like any peripheral: its platform, settings and peripherals."""

import json
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from support import run, settle

from majordom_integration_sdk import AbstractController, Endpoint, IntegrationError, Need, Peripheral, Platform, Service
from majordom_integration_sdk.runtime import HostNetwork, MemoryDisabledStore, PeripheralState, PeripheralStatus
from majordom_integration_sdk.runtime import StatusKind as K
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter
from majordom_integration_sdk.testing import FakeCompose, FakePeripheral, RecordingControllerOutput, build_test_platform

DEVICE = uuid4()


class Greeting(BaseModel):
    greeting: str = "hello"


def echo_server() -> Service[Greeting]:
    return Service(
        "echo-server",
        compose={"image": "echo", "command": ["--greeting", "{settings.greeting}"]},
        needs=[Need("ip")],
        provides=[Endpoint("example", "echo", "ws://127.0.0.1:{ports.api}")],
        settings=Greeting,
        ports={"api": 5600},
    )


class EchoController(AbstractController[Device, Parameter, "EchoController.Settings"]):
    needs = [Need("example", ["echo"])]

    class Settings(BaseModel):
        is_loud: bool = False

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


def integration() -> tuple[Service[Greeting], EchoController]:
    """What the integration exports, given its platform as by the Hub."""
    server, echo = peripherals(build_test_platform())
    assert isinstance(server, Service) and isinstance(echo, EchoController)
    return cast(Service[Greeting], server), echo


async def test_an_integration_runs_once_the_server_it_provides_serves(tmp_path):
    server, echo = integration()
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo)

    assert runtime.status(echo) == PeripheralStatus(PeripheralState.RUNNING)
    assert echo.started_with == [("ws://127.0.0.1:5600", False)]  # its needs bound, its settings given, before start
    assert runtime.users(server) == ["echo"] and echo.key == "echo"  # known by its slug


async def test_an_integration_is_kept_by_its_slug_and_shown_by_its_name(tmp_path):
    class ZWaveJS(EchoController):
        name = "Z-Wave JS"

    store, server, echo = MemoryDisabledStore(), echo_server(), ZWaveJS(build_test_platform())
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo, disabled=store)
    await runtime.update_settings(echo, {"is_loud": True})
    await runtime.disable(echo)

    assert (echo.key, echo.name) == ("z-wave-js", "Z-Wave JS")
    documents = tmp_path / "data" / "peripherals" / "z-wave-js"
    assert store.is_disabled("z-wave-js") and echo.platform.documents_folder(echo) == documents
    assert json.loads((tmp_path / "data" / "state.json").read_text())["settings"]["z-wave-js"] == {"is_loud": True}
    assert runtime.peripheral("z-wave-js") is echo


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


# what a controller tells, through the one output every peripheral shares


async def test_every_peripheral_shares_one_output_and_a_controllers_reports_reach_the_hub(tmp_path):
    hub, server, echo = RecordingControllerOutput(), *integration()
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo, controller_output=hub)
    assert echo.output is server.output and not hasattr(echo, "runtime")

    await echo.output.controller_did_lose_device(echo, DEVICE)
    await echo.output.controller_did_encounter_error(echo, IntegrationError("server-slow", "The server is slow."))
    assert hub.lost_devices == [DEVICE] and list(hub.errors) == ["server-slow"]
    assert runtime.status(echo).state == PeripheralState.RUNNING  # it goes on


async def test_a_controller_that_cant_go_on_says_why_and_is_tried_again(tmp_path):
    hub, server, echo = RecordingControllerOutput(), *integration()
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, echo, controller_output=hub)

    lost = IntegrationError("radio-lost", "The radio is gone: plug it in again.", is_still_running=False)
    await echo.output.controller_did_encounter_error(echo, lost)
    await settle(runtime)
    assert hub.errors == {"radio-lost": lost}  # the user sees it
    status = runtime.status(echo)
    assert (status.state, status.kind, status.detail.split(";")[0]) == (
        PeripheralState.WAITING,
        K.RETRYING,
        lost.message,
    )


async def test_what_a_controller_provides_is_read_again_when_it_says_so(tmp_path):
    class ESPHome(EchoController):
        name = "esphome"

        def __init__(self, platform: Platform):
            super().__init__(platform)
            self.proxies: list[Peripheral] = []

        @property
        def provides(self) -> list[Peripheral]:
            return self.proxies

    server, esphome = echo_server(), ESPHome(build_test_platform())
    runtime = await run(tmp_path, HostNetwork(), FakeCompose(), server, esphome)

    esphome.proxies.append(proxy := FakePeripheral("aa:bb"))  # a node connected
    await esphome.output.controller_did_change_provided(esphome)
    await settle(runtime)
    assert runtime.peripheral("esphome/aa:bb") is proxy and proxy.output is esphome.output


async def test_without_a_runtime_a_controller_logs_what_it_tells_or_reports_to_the_output_given(caplog):
    echo = EchoController(build_test_platform())  # library mode
    await echo.output.controller_did_encounter_error(echo, IntegrationError("down", "Down.", is_still_running=False))
    assert "Down." in caplog.text

    echo.output = hub = RecordingControllerOutput()
    await echo.output.controller_did_change_provided(echo)
    assert hub.provided_changes == [echo]
