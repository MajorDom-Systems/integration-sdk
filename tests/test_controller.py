"""A minimal concrete controller exercises the framework end to end with test doubles."""

from uuid import UUID

from conftest import make_device_state

from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials
from majordom_integration_sdk.schemas.event import DeviceParameterChange
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_dependencies


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


def test_name_slug_and_uuid_helpers():
    controller = _MinimalController(build_test_dependencies())
    assert controller.name_slug == "example-protocol"
    # Deterministic, namespaced, and hierarchical.
    device_uuid = controller.device_uuid("abc")
    assert controller.device_uuid("abc") == device_uuid
    assert controller.parameter_uuid(device_uuid, "p1") != device_uuid


def test_documents_folder_is_the_injected_path(tmp_path):
    deps = build_test_dependencies(documents_folder=tmp_path)
    controller = _MinimalController(deps)
    assert controller.documents_folder == tmp_path
    assert controller.documents_folder.exists()


async def test_controller_can_use_injected_repository():
    deps = build_test_dependencies()
    controller = _MinimalController(deps)
    device = make_device_state(integration=controller.name_slug)
    async with controller.dependencies.make_device_repository() as repo:
        await repo.save(device)
        stored = await repo.state(device.id)
        assert stored is not None and stored.id == device.id


async def test_recording_output_captures_reports():
    deps = build_test_dependencies()
    controller = _MinimalController(deps)
    output = deps.output
    assert isinstance(output, RecordingControllerOutput)

    device = make_device_state()
    change = DeviceParameterChange(device_id=device.id, parameter_id=device.parameters[0].id, value=1)
    await output.controller_did_connect_device(controller, device.id)
    await output.controller_did_receive_events(controller, [change])

    assert output.connected_devices == [device.id]
    assert output.events == [change]
