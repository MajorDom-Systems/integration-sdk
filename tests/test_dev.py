"""Smoke tests for the standalone dev runner (``majordom_integration_sdk.dev``).

``run_controller`` wires real, network-live discovery services and then blocks forever, so it is
never run whole in CI. These tests cover the parts that can break without a device on the
network: ``build_dependencies`` assembles a correctly-scoped dependency set, and
``run_controller`` starts and stops a controller cleanly (discovery services faked out).
"""

from uuid import UUID

from conftest import make_device_state

import majordom_integration_sdk.dev as dev
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.dev import LoggingControllerOutput, build_dependencies, run_controller
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.testing import (
    FakeBLEDiscoveryService,
    FakeSSDPDiscoveryService,
    FakeZeroconfDiscoveryService,
)


class _RecordingController(AbstractController[Device, Parameter]):
    """Minimal controller that records that it was started and stopped."""

    name = "Dev Example"
    instances: list["_RecordingController"] = []

    def __init__(self, deps):
        super().__init__(deps)
        self.started = False
        self.stopped = False
        _RecordingController.instances.append(self)

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

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


# --- build_dependencies -------------------------------------------------------------------


def test_build_dependencies_defaults_to_memory_and_logging_output(tmp_path):
    deps = build_dependencies(storage_root=tmp_path, integration="Example")

    assert isinstance(deps.output, LoggingControllerOutput)
    # Real (network-live) discovery services are wired for a standalone run.
    assert isinstance(deps.zeroconf_discovery_service, dev.ZeroconfDiscoveryService)
    assert isinstance(deps.ssdp_discovery_service, dev.SSDPDiscoveryService)
    assert isinstance(deps.ble_discovery_service, dev.BLEDiscoveryService)


def test_build_dependencies_scopes_documents_folder(tmp_path):
    # slug wins over integration for the subfolder name; the folder is created eagerly.
    deps = build_dependencies(storage_root=tmp_path, integration="Example", slug="example-protocol")
    assert deps.documents_folder == tmp_path / "example-protocol"
    assert deps.documents_folder.is_dir()


def test_build_dependencies_honors_custom_output(tmp_path):
    from majordom_integration_sdk.testing import RecordingControllerOutput

    output = RecordingControllerOutput()
    deps = build_dependencies(storage_root=tmp_path, output=output)
    assert deps.output is output


async def test_build_dependencies_memory_repository_round_trips(tmp_path):
    deps = build_dependencies(storage_root=tmp_path, integration="example")
    device = make_device_state(integration="example")
    async with deps.make_device_repository() as repo:
        await repo.save(device)
        assert (await repo.state(device.id)) is not None


async def test_build_dependencies_sqlite_persists_to_file(tmp_path):
    db_path = tmp_path / "dev.sqlite"
    device = make_device_state(integration="example")

    deps = build_dependencies(storage_root=tmp_path, db_path=db_path, integration="example")
    async with deps.make_device_repository() as repo:
        await repo.save(device)

    assert db_path.exists(), "a db_path should select the file-backed sqlite repository"
    # A fresh dependency set over the same file sees the persisted device (survives 'restarts').
    deps2 = build_dependencies(storage_root=tmp_path, db_path=db_path, integration="example")
    async with deps2.make_device_repository() as repo:
        assert (await repo.state(device.id)) is not None


# --- run_controller -----------------------------------------------------------------------


async def test_run_controller_starts_and_stops_cleanly(tmp_path, monkeypatch):
    """run_controller should start the controller + discovery services and, on cancellation,
    stop everything without raising. Discovery services are faked so nothing touches the network."""
    import asyncio

    monkeypatch.setattr(dev, "ZeroconfDiscoveryService", FakeZeroconfDiscoveryService)
    monkeypatch.setattr(dev, "SSDPDiscoveryService", FakeSSDPDiscoveryService)
    monkeypatch.setattr(dev, "BLEDiscoveryService", FakeBLEDiscoveryService)
    _RecordingController.instances.clear()

    task = asyncio.create_task(run_controller(_RecordingController, storage_root=tmp_path))

    async with asyncio.timeout(5):
        while not (_RecordingController.instances and _RecordingController.instances[-1].started):
            await asyncio.sleep(0.01)

    task.cancel()
    await task  # run_controller swallows the cancellation after cleaning up

    controller = _RecordingController.instances[-1]
    assert controller.started and controller.stopped
    assert task.done() and task.exception() is None
