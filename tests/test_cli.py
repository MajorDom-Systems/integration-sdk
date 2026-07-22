"""Exercises the interactive CLI's controller-driving core (`_CliSession`) against a fake
controller wired with the offline test dependencies — no REPL, no network. The typer-shell
wiring on top is a thin layer over these same calls."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.dev.cli import _CliControllerOutput, _CliSession, _print
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import (
    CredentialsType,
    Device,
    DeviceState,
    Discovery,
    ProvidedCredentials,
)
from majordom_integration_sdk.schemas.event import DeviceParameterChange
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterVisibility,
)
from majordom_integration_sdk.testing import build_test_dependencies

INTEGRATION = "Fake"


def _param(role: ParameterRole = ParameterRole.control) -> ParameterState:
    return ParameterState(
        id=uuid4(),
        name="On",
        data_type=ParameterDataType.bool,
        role=role,
        visibility=ParameterVisibility.user,
        integration_data=None,
    )


class FakeController(AbstractController):
    """Minimal controller: pairing persists a device with one control parameter; every other
    method just records that it was called with the resolved object."""

    name = INTEGRATION

    def __init__(self, dependencies, *, param_role: ParameterRole = ParameterRole.control):
        super().__init__(dependencies)
        self._discoveries: dict[UUID, Discovery] = {}
        self._param_role = param_role
        self.paired: list[UUID] = []
        self.sent: list[tuple] = []
        self.identified: list[UUID] = []
        self.fetched: list[UUID] = []
        self.unpaired: list[UUID] = []

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self._discoveries

    async def start(self):
        pass

    async def stop(self):
        pass

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None):
        device_id = uuid4()
        state = DeviceState(
            id=device_id,
            name=discovery.device_name,
            room_id=uuid4(),
            transport=discovery.transport,
            integration=self.name,
            manufacturer=None,
            parameters=[_param(self._param_role)],
        )
        async with self.dependencies.make_device_repository() as repo:
            await repo.save(state)
        self.paired.append(device_id)
        return device_id

    async def unpair(self, device: Device):
        self.unpaired.append(device.id)

    async def identify(self, device: Device):
        self.identified.append(device.id)

    async def fetch(self, device: Device):
        self.fetched.append(device.id)

    async def send_command(self, command: DeviceCommand, device: Device, parameter):
        self.sent.append((command.device_id, command.parameter_id, command.value))


def _discovery() -> Discovery:
    return Discovery(
        id=uuid4(),
        integration=INTEGRATION,
        expected_credentials_options=[CredentialsType.none],
        transport="test",
        device_manufacturer=None,
        device_name="Bulb",
        device_category=None,
        device_icon=None,
    )


@pytest.fixture
def session_and_controller():
    deps = build_test_dependencies(integration=INTEGRATION)
    controller = FakeController(deps)
    return _CliSession(controller), controller


async def test_pair_list_control_unpair_flow(session_and_controller):
    session, controller = session_and_controller

    discovery = _discovery()
    controller.discoveries[discovery.id] = discovery

    # pair -> persists a device
    device_id = await session.pair(discovery.id, "none", "")
    assert device_id in controller.paired

    # devices -> lists it
    devices = await session.list_devices()
    assert [d.id for d in devices] == [device_id]

    # device -> shows its (single control) parameter
    state = await session.device_state(device_id)
    assert state is not None
    assert len(state.parameters) == 1
    parameter = state.parameters[0]
    assert parameter.role == ParameterRole.control

    # control -> resolves device+parameter and reaches the controller
    assert await session.control(device_id, parameter.id, True) == "Command sent"
    assert controller.sent == [(device_id, parameter.id, True)]

    # identify / fetch reach the controller with the resolved device
    assert await session.identify(device_id) is True
    assert await session.fetch(device_id) is True
    assert controller.identified == [device_id]
    assert controller.fetched == [device_id]

    # unpair
    assert await session.unpair(device_id) is True
    assert controller.unpaired == [device_id]


async def test_pair_unknown_discovery_returns_none(session_and_controller):
    session, _ = session_and_controller
    assert await session.pair(uuid4()) is None


async def test_actions_on_missing_device(session_and_controller):
    session, controller = session_and_controller
    missing = uuid4()
    assert (await session.control(missing, uuid4(), True)).startswith("No device")
    assert await session.identify(missing) is False
    assert await session.fetch(missing) is False
    assert await session.unpair(missing) is False
    assert controller.sent == controller.identified == []


async def test_control_rejects_non_control_parameter():
    deps = build_test_dependencies(integration=INTEGRATION)
    controller = FakeController(deps, param_role=ParameterRole.sensor)
    session = _CliSession(controller)

    discovery = _discovery()
    controller.discoveries[discovery.id] = discovery
    device_id = await session.pair(discovery.id)
    parameter = (await session.device_state(device_id)).parameters[0]

    message = await session.control(device_id, parameter.id, True)
    assert "not controllable" in message
    assert controller.sent == []  # never reached the controller


async def test_output_delegate_persists_event_values():
    deps = build_test_dependencies(integration=INTEGRATION)
    controller = FakeController(deps)
    session = _CliSession(controller)

    discovery = _discovery()
    controller.discoveries[discovery.id] = discovery
    device_id = await session.pair(discovery.id)
    parameter = (await session.device_state(device_id)).parameters[0]

    output = _CliControllerOutput(deps.make_device_repository)
    await output.controller_did_receive_events(
        controller, [DeviceParameterChange(device_id=device_id, parameter_id=parameter.id, value=True)]
    )

    async with deps.make_device_repository() as repo:
        stored = await repo.get_parameter_state(device_id, parameter.id)
    assert stored is not None
    assert stored.value  # a (non-empty, encoded) value was written back by the event


def test_prompt_safe_print_is_a_noop_safe_call(capsys):
    # Not a TTY under pytest, so _print falls back to a plain print (no escape codes leaking).
    _print("hello", "world")
    out = capsys.readouterr().out
    assert "hello" in out and "world" in out
    assert "\x1b[" not in out  # no cursor-magic when stdout isn't a terminal


def test_run_cli_is_lazily_exported():
    from majordom_integration_sdk import dev

    assert callable(dev.run_cli)


def test_capturing_handler_buffers_then_dumps(capsys):
    import logging

    from majordom_integration_sdk.dev.cli import _CapturingHandler

    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log = logging.getLogger("test.capture")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    log.warning("chatty controller noise")

    # buffered, not printed — the TUI stays clean during the session
    assert capsys.readouterr().err == ""
    assert len(handler.records) == 1

    # dumped to stderr on exit for debugging
    handler.dump()
    err = capsys.readouterr().err
    assert "chatty controller noise" in err
    assert "captured logs" in err
