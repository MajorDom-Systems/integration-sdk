"""A minimal concrete controller exercises the framework end to end with test doubles."""

import inspect
from uuid import UUID

import pytest
from factories import make_device_state

from majordom_integration_sdk import NoSettings
from majordom_integration_sdk.controller import AbstractController, ControllerOutput, LoggingControllerOutput
from majordom_integration_sdk.repository import DeviceRepositoryMemory
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.schemas.event import DeviceParameterChange
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_platform


class _MinimalController(AbstractController[Device, Parameter]):
    name = "Example Protocol"

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        pass

    async def stop(self):
        pass

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


def test_key_and_uuid_helpers():
    controller = _MinimalController(build_test_platform())
    assert controller.key == "example-protocol"
    # Deterministic, namespaced, and hierarchical.
    device_uuid = controller.device_uuid("abc")
    assert controller.device_uuid("abc") == device_uuid
    assert controller.parameter_uuid(device_uuid, "p1") != device_uuid


def test_its_documents_folder_is_its_own_under_the_platforms(tmp_path):
    controller = _MinimalController(build_test_platform(documents_root=tmp_path))
    assert controller.platform.documents_folder(controller) == tmp_path / "example-protocol"
    assert (tmp_path / "example-protocol").is_dir()


async def test_its_device_repository_holds_only_its_own_devices():
    controller = _MinimalController(build_test_platform())
    device = make_device_state(integration=controller.key)  # its slug, "example-protocol"
    async with controller.platform.device_repository(controller) as repo:
        await repo.save(device)
        stored = await repo.state(device.id)
        assert stored is not None and stored.id == device.id
        with pytest.raises(PermissionError):
            await repo.save(make_device_state(integration="Other"))


async def test_recording_output_captures_reports():
    controller = _MinimalController(build_test_platform())
    output = controller.output = RecordingControllerOutput()

    device = make_device_state()
    change = DeviceParameterChange(device_id=device.id, parameter_id=device.parameters[0].id, value=1)
    await output.controller_did_connect_device(controller, device.id)
    await output.controller_did_receive_events(controller, [change])

    assert output.connected_devices == [device.id]
    assert output.events == [change]


def test_name_is_autoderived_from_class_name():
    """`name` defaults to the class name (CamelCase → spaced), overridable with an explicit `name`."""

    class HueController(AbstractController[Device, Parameter]):
        pass

    class ZigBeeController(AbstractController[Device, Parameter]):
        name = "ZigBee"  # override the "Zig Bee" default

    assert HueController.name == "Hue"
    assert HueController.slug() == "hue"
    assert ZigBeeController.name == "ZigBee"
    assert ZigBeeController.slug() == "zigbee"


async def test_the_pairing_window_opens_without_credentials():
    # the Hub broadcasts `start_pairing_window(duration)`: credentials are only for the one integration that needs them
    controller = _MinimalController(build_test_platform())
    await controller.start_pairing_window(5)


async def test_changed_settings_restart_it_unless_handled_and_the_platform_facts_dont():
    controller = _MinimalController(build_test_platform())
    assert await controller.settings_did_change(NoSettings()) is False
    assert await controller.platform_facts_did_change(build_test_platform(region="Europe")) is True


async def test_a_test_platform_holds_the_facts_given_and_its_store_every_device():
    store: dict = {}
    platform = build_test_platform(region="Europe", thread_dataset="0e08", store=store)
    controller = _MinimalController(platform)
    async with platform.device_repository(controller) as repo:
        await repo.save(make_device_state(integration=controller.key))

    assert (platform.region, platform.wifi, platform.thread_dataset) == ("Europe", None, "0e08")
    async with DeviceRepositoryMemory(store=store).session() as everything:
        assert [device.integration for device in await everything.get_all()] == ["example-protocol"]


def test_the_output_annotations_evaluate():
    """A host reads them, e.g. the Hub's null output: each names the controller it hears from."""
    for output in (ControllerOutput, LoggingControllerOutput):
        for name, method in inspect.getmembers(output, inspect.isfunction):
            if name.startswith("controller_did_"):
                assert inspect.get_annotations(method, eval_str=True)["controller"] is AbstractController, name
