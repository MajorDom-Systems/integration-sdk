"""Smoke tests for the standalone dev runner (``majordom_integration_sdk.dev``).

``run_controller`` binds needs through the runtime and then blocks forever, so it is never run whole
against real radios in CI. These tests cover the parts that can break without a device on the
network: ``build_platform`` assembles a correctly-scoped platform, the `Runner` runs a controller
under the runtime as the Hub does (among fake peripherals), and ``run_controller`` starts and stops
cleanly.
"""

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from factories import make_device_state

import majordom_integration_sdk.dev as dev
from majordom_integration_sdk import Endpoint, Need, PeripheralChange
from majordom_integration_sdk.capabilities import BLE_ROUTER
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.dev import LoggingControllerOutput, Runner, build_platform, build_runtime, run_controller
from majordom_integration_sdk.runtime import Pinned, Runtime
from majordom_integration_sdk.runtime.output import HostOutput
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.testing import FakeBLERouter, FakePeripheral, FakeSSDP

ZWAVE = Endpoint("zwave", "zwave-js-server", "ws://127.0.0.1:3000", is_shared=True)


class _RecordingController(AbstractController[Device, Parameter]):
    """Minimal controller that records that it was started and stopped, and the peripheral changes it saw."""

    name = "Dev Example"
    needs = [Need("zwave", ["zwave-js-server"]), Need("ble", ["router"], is_required=False)]
    instances: list["_RecordingController"] = []
    handles_changes = True

    def __init__(self, platform):
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


def peripherals(platform) -> list[_RecordingController]:
    """What its integration package exports."""
    return [_RecordingController(platform)]


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
        platform=controller.platform,
    )
    return Runner(controller, runtime), controller, runtime


async def until(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.01)


# --- build_platform -----------------------------------------------------------------------


def test_build_platform_roots_every_peripherals_documents_folder(tmp_path):
    platform = build_platform(storage_root=tmp_path)
    assert platform.documents_root == tmp_path and tmp_path.is_dir()


async def test_build_platform_memory_repository_round_trips_in_its_scope(tmp_path):
    platform = build_platform(storage_root=tmp_path)
    device = make_device_state(integration="example")
    async with platform.make_device_repository("example") as repo:
        await repo.save(device)
    async with platform.make_device_repository("example") as repo:
        assert (await repo.state(device.id)) is not None
    async with platform.make_device_repository("other") as repo:
        assert (await repo.state(device.id)) is None


async def test_build_platform_sqlite_persists_to_file(tmp_path):
    db_path = tmp_path / "dev.sqlite"
    device = make_device_state(integration="example")

    platform = build_platform(storage_root=tmp_path, db_path=db_path)
    async with platform.make_device_repository("example") as repo:
        await repo.save(device)

    assert db_path.exists(), "a db_path should select the file-backed sqlite repository"
    # A fresh platform over the same file sees the persisted device (survives 'restarts').
    again = build_platform(storage_root=tmp_path, db_path=db_path)
    async with again.make_device_repository("example") as repo:
        assert (await repo.state(device.id)) is not None


# --- the runtime --------------------------------------------------------------------------


def test_without_the_runtime_extra_the_runner_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(dev.importlib.util, "find_spec", lambda name: None)
    platform = build_platform(storage_root=tmp_path)
    with pytest.raises(ImportError, match=r"majordom-integration-sdk\[runtime"):
        build_runtime([_RecordingController(platform)], tmp_path, platform=platform)


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
    assert controller.peripherals.instance(BLE_ROUTER) is router  # the router, as under the Hub

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
    appeared = PeripheralChange("ble", None, router.endpoint)  # the router started beside it, at boot
    assert controller.changes == [appeared, PeripheralChange("zwave", ZWAVE, moved)]
    assert (controller.started, controller.stopped) == (1, 0)  # handled in place

    controller.handles_changes = False
    await pinned.pin(ZWAVE)
    await until(lambda: controller.started == 2)
    assert controller.stopped == 1
    await runner_.stop()


async def test_run_controller_with_peripherals_given_runs_without_the_runtime(tmp_path):
    _RecordingController.instances.clear()
    task = asyncio.create_task(run_controller(peripherals, storage_root=tmp_path, peripherals={"zwave": ZWAVE}))

    async with asyncio.timeout(5):
        while not (_RecordingController.instances and _RecordingController.instances[-1].started):
            await asyncio.sleep(0.01)

    task.cancel()
    await task  # run_controller swallows the cancellation after cleaning up

    controller = _RecordingController.instances[-1]
    assert controller.started and controller.stopped
    assert controller.peripherals.endpoint("zwave") == ZWAVE
    assert controller.platform.documents_folder(controller) == tmp_path / "dev-example"  # by its key, as by the Hub
    assert isinstance(controller.output, LoggingControllerOutput)  # what it reports is logged
    assert not (tmp_path / "runtime").exists()


async def test_run_controller_wants_one_controller_among_what_an_integration_exports(tmp_path):
    with pytest.raises(ValueError, match="one controller, not 2"):
        await run_controller(lambda platform: peripherals(platform) * 2, storage_root=tmp_path)


async def test_run_controller_starts_and_stops_cleanly_under_the_runtime(tmp_path):
    _RecordingController.instances.clear()
    task = asyncio.create_task(
        run_controller(
            peripherals,
            storage_root=tmp_path,
            runtime_peripherals=[FakeSSDP()],
            pinned=[ZWAVE],
            region="Europe",
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
    assert controller.platform.region == "Europe"  # the platform it was given, its overrides too
    output = controller.output  # the runtime's, which logs what it reports
    assert isinstance(output, HostOutput) and isinstance(output.controller_output, LoggingControllerOutput)


async def test_what_a_peripheral_learns_reaches_the_controllers_platform(tmp_path, router):
    border_router = FakePeripheral("my-border-router")
    controller = _RecordingController(replace(build_platform(storage_root=tmp_path), region="Europe"))
    runtime = build_runtime(
        [controller], tmp_path / "runtime", runtime_peripherals=[border_router], platform=controller.platform
    )
    await runtime.start()

    await border_router.output.peripheral_did_learn(border_router, {"thread_dataset": "0e08"})
    assert (controller.platform.region, controller.platform.thread_dataset) == ("Europe", "0e08")
    await runtime.stop()
