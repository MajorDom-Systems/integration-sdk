"""Smoke tests for the standalone dev runner (``majordom_integration_sdk.dev``).

``run_controller`` binds needs through the runtime and then blocks forever, so it is never run whole
against real radios in CI. These tests cover the parts that can break without a device on the
network: ``build_platform`` assembles a correctly-scoped platform, the `Runner` runs a controller
under the runtime as the Hub does (among fake peripherals), and ``run_controller`` starts and stops
cleanly.
"""

import asyncio
from uuid import UUID

import pytest
from factories import make_device_state

import majordom_integration_sdk.dev as dev
from majordom_integration_sdk import Endpoint, Need, PeripheralChange
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.dev import LoggingControllerOutput, Runner, build_platform, build_runtime, run_controller
from majordom_integration_sdk.runtime import Pinned, Runtime
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.testing import FakeBLERouter, FakeSSDP

ZWAVE = Endpoint("zwave", "zwave-js-server", "ws://127.0.0.1:3000", is_shared=True)


class _RecordingController(AbstractController[Device, Parameter]):
    """Minimal controller that records that it was started and stopped, and the peripheral changes it saw."""

    name = "Dev Example"
    needs = [Need("zwave", ["zwave-js-server"]), Need("ble", ["router"], is_required=False)]
    instances: list["_RecordingController"] = []
    handles_changes = True

    def __init__(self, platform=None):
        super().__init__(platform)
        self.started = self.stopped = 0
        self.changes: list[PeripheralChange] = []
        _RecordingController.instances.append(self)

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        self.changes.append(change)
        return self.handles_changes

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None):
        pass

    async def unpair(self, device: Device):
        pass

    async def identify(self, device: Device):
        pass

    async def fetch(self, device: Device):
        pass

    async def send_command(self, command: DeviceCommand, device: Device, parameter: Parameter):
        pass


@pytest.fixture
def router() -> FakeBLERouter:
    return FakeBLERouter()


def runner(tmp_path, router: FakeBLERouter, pinned=()) -> tuple[Runner, _RecordingController, Runtime]:
    """A controller under the runtime, among peripherals without radios or sockets."""
    controller = _RecordingController(build_platform(storage_root=tmp_path))
    runtime = build_runtime(
        [controller],
        tmp_path / "runtime",
        runtime_peripherals=[router, FakeSSDP()],
        pinned=pinned,
    )
    return Runner(controller, runtime), controller, runtime


async def until(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.01)


# --- build_platform -----------------------------------------------------------------------


def test_build_platform_defaults_to_memory_and_logging_output(tmp_path):
    assert isinstance(build_platform(storage_root=tmp_path, integration="Example").output, LoggingControllerOutput)


def test_build_platform_scopes_documents_folder(tmp_path):
    # slug wins over integration for the subfolder name; the folder is created eagerly.
    platform = build_platform(storage_root=tmp_path, integration="Example", slug="example-protocol")
    assert platform.documents_folder == tmp_path / "example-protocol"
    assert platform.documents_folder.is_dir()


def test_build_platform_honors_custom_output(tmp_path):
    from majordom_integration_sdk.testing import RecordingControllerOutput

    output = RecordingControllerOutput()
    assert build_platform(storage_root=tmp_path, output=output).output is output


async def test_build_platform_memory_repository_round_trips(tmp_path):
    platform = build_platform(storage_root=tmp_path, integration="example")
    device = make_device_state(integration="example")
    async with platform.make_device_repository() as repo:
        await repo.save(device)
        assert (await repo.state(device.id)) is not None


async def test_build_platform_sqlite_persists_to_file(tmp_path):
    db_path = tmp_path / "dev.sqlite"
    device = make_device_state(integration="example")

    platform = build_platform(storage_root=tmp_path, db_path=db_path, integration="example")
    async with platform.make_device_repository() as repo:
        await repo.save(device)

    assert db_path.exists(), "a db_path should select the file-backed sqlite repository"
    # A fresh platform over the same file sees the persisted device (survives 'restarts').
    again = build_platform(storage_root=tmp_path, db_path=db_path, integration="example")
    async with again.make_device_repository() as repo:
        assert (await repo.state(device.id)) is not None


# --- the runtime --------------------------------------------------------------------------


def test_without_the_runtime_extra_the_runner_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(dev.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ImportError, match=r"majordom-integration-sdk\[runtime"):
        build_runtime([_RecordingController(build_platform(storage_root=tmp_path))], tmp_path)


async def test_the_runner_starts_the_controller_once_its_needs_are_bound(tmp_path, router):
    runner_, controller, runtime = runner(tmp_path, router)
    await runner_.start()
    assert not controller.started  # nothing offers zwave-js-server here
    assert "zwave" in runtime.unbound(controller)
    pinned = runtime.peripherals[0]
    assert isinstance(pinned, Pinned)

    await pinned.pin(ZWAVE)
    await until(lambda: runner_.is_running)
    assert controller.peripherals.endpoint("zwave") == ZWAVE
    assert controller.peripherals.instance("ble") is router  # the router, as under the Hub

    await pinned.unpin("zwave")  # a required need lost: stopped until it's back
    assert controller.stopped == 1 and not runner_.is_running
    await runner_.stop()


async def test_the_runner_hands_a_moved_peripheral_over_or_restarts(tmp_path, router):
    runner_, controller, runtime = runner(tmp_path, router, pinned=[ZWAVE])
    await runner_.start()
    await until(lambda: runner_.is_running)
    pinned = runtime.peripherals[0]
    assert isinstance(pinned, Pinned)
    moved = Endpoint("zwave", "zwave-js-server", "ws://192.168.1.9:3000", is_shared=True)

    await pinned.pin(moved)
    assert controller.changes == [PeripheralChange("zwave", ZWAVE, moved)]
    assert (controller.started, controller.stopped) == (1, 0)  # handled in place

    controller.handles_changes = False
    await pinned.pin(ZWAVE)
    await until(lambda: controller.started == 2)
    assert controller.stopped == 1
    await runner_.stop()


async def test_run_controller_with_peripherals_given_runs_without_the_runtime(tmp_path):
    _RecordingController.instances.clear()
    task = asyncio.create_task(
        run_controller([_RecordingController()], storage_root=tmp_path, peripherals={"zwave": ZWAVE})
    )

    async with asyncio.timeout(5):
        while not (_RecordingController.instances and _RecordingController.instances[-1].started):
            await asyncio.sleep(0.01)

    task.cancel()
    await task  # run_controller swallows the cancellation after cleaning up

    controller = _RecordingController.instances[-1]
    assert controller.started and controller.stopped
    assert controller.peripherals.endpoint("zwave") == ZWAVE
    assert controller.platform.documents_folder == tmp_path / "dev-example"  # given its platform, as by the Hub
    assert not (tmp_path / "runtime").exists()


async def test_run_controller_wants_one_controller_among_what_an_integration_exports(tmp_path):
    with pytest.raises(ValueError, match="one controller, not 2"):
        await run_controller([_RecordingController(), _RecordingController()], storage_root=tmp_path)


async def test_run_controller_starts_and_stops_cleanly_under_the_runtime(tmp_path):
    _RecordingController.instances.clear()
    task = asyncio.create_task(
        run_controller(
            [_RecordingController()],
            storage_root=tmp_path,
            runtime_peripherals=[FakeSSDP()],
            pinned=[ZWAVE],
        )
    )

    async with asyncio.timeout(5):
        while not (_RecordingController.instances and _RecordingController.instances[-1].started):
            await asyncio.sleep(0.01)

    task.cancel()
    await task

    controller = _RecordingController.instances[-1]
    assert controller.started and controller.stopped
    assert controller.peripherals.endpoint("zwave") == ZWAVE  # bound by the runtime
