from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from majordom_integration_sdk import (
    AbstractController,
    Context,
    Endpoint,
    Need,
    NoSettings,
    Peripheral,
    PeripheralChange,
    Peripherals,
    Service,
    UsbDriver,
)
from majordom_integration_sdk.peripherals.peripheral import START_TIMEOUT
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter
from majordom_integration_sdk.testing import build_test_platform


def test_need_accepts_family_and_dialect():
    assert Need("zigbee").accepts(Endpoint("zigbee", "znp"))
    assert Need("zigbee", ["ezsp", "znp"]).accepts(Endpoint("zigbee", "znp"))
    assert not Need("zigbee", ["ezsp"]).accepts(Endpoint("zigbee", "znp"))
    assert not Need("thread").accepts(Endpoint("zigbee", "ezsp"))


class Host(Peripheral):
    name = "host"
    provides = [Endpoint("ip", is_shared=True)]


async def test_a_peripheral_needs_provides_and_is_bound_nothing_by_default():
    host = Host()
    assert (host.needs, host.provides, host.peripherals.bound) == ((), [Endpoint("ip", is_shared=True)], {})
    with pytest.raises(TypeError):
        cast(Any, host.peripherals.bound)["ip"] = Endpoint("ip")  # shared by every peripheral until bound: read-only
    assert not host.is_on_demand and host.start_timeout == START_TIMEOUT
    assert host.Settings is NoSettings  # it takes none
    await host.start()
    await host.stop()
    await host.runtime.peripheral_did_change_provided(host)  # nothing runs it: told no one
    assert await host.needed_peripheral_did_change(PeripheralChange("ip", None, None)) is False  # restart it
    assert await host.settings_did_change(NoSettings()) is False
    assert await host.context_did_change(Context()) is True  # the context concerns few


def test_peripherals_hand_out_one_endpoint_many_or_an_instance():
    router = object()
    adapters = [Endpoint("ble", "bluez", "hci0"), Endpoint("ble", "bluez", "hci1")]
    peripherals = Peripherals({"zigbee": Endpoint("zigbee", "ezsp", "/dev/ttyUSB0"), "raw": adapters})
    assert peripherals.endpoint("zigbee").url == "/dev/ttyUSB0"
    assert peripherals.endpoints("zigbee") == [peripherals.bound["zigbee"]]
    assert peripherals.endpoints("raw") == adapters
    with pytest.raises(TypeError, match="endpoints"):
        peripherals.endpoint("raw")
    assert peripherals.endpoints("thread") == []
    assert peripherals.instance("ble") is None
    with pytest.raises(KeyError):
        peripherals.endpoint("thread")

    peripherals = Peripherals({"ble": Endpoint("ble", "router", "router", is_shared=True, instance=router)})
    assert peripherals.instance("ble") is router


def test_an_endpoint_is_the_same_whatever_instance_it_carries():
    assert Endpoint("ble", "esphome-proxy", "esphome:aa", instance=object()) == Endpoint(
        "ble", "esphome-proxy", "esphome:aa", instance=object()
    )
    assert not Need("ble").is_many


def test_endpoint_params_say_how_to_talk_to_it():
    stick = Endpoint("zigbee", "ezsp", "/dev/ttyUSB0", params={"baud": 115200, "flow_control": True})
    assert stick.params["baud"] == 115200
    assert Endpoint("zigbee").params == {}
    assert hash(stick) == hash(Endpoint("zigbee", "ezsp", "/dev/ttyUSB0"))


SERVER = Service("server", compose={}, provides=[Endpoint("matter", "matter-server", "ws://127.0.0.1:{ports.api}")])


async def test_a_service_runs_on_demand_and_hooks_in_doing_nothing():
    assert SERVER.is_on_demand and SERVER.start_timeout == 30 * 60  # long enough to pull its image first
    assert SERVER.ports == {} and SERVER.Settings is NoSettings
    assert await SERVER.provision(Endpoint("matter", "matter-server", "ws://127.0.0.1:5580")) is None
    assert await SERVER.is_healthy()


async def test_without_a_runtime_a_service_provides_its_templates_and_doesnt_run():
    assert SERVER.provides == SERVER.templates
    with pytest.raises(NotImplementedError, match="run server yourself"):
        await SERVER.start()


class Silabs(UsbDriver):
    name = "silabs"
    firmwares = {"zigbee-ncp": [Endpoint("zigbee", "ezsp", "{device}", params={"baud": 115200})]}


def test_a_usb_driver_says_what_a_firmware_serves_and_offers_itself():
    driver = Silabs()
    assert driver.serves("zigbee-ncp", "/dev/ttyUSB0", {"flow_control": False}) == [
        Endpoint("zigbee", "ezsp", "/dev/ttyUSB0", params={"baud": 115200, "flow_control": False})
    ]
    assert driver.serves(None, "/dev/ttyUSB0") == []
    assert driver.provides[0].instance is driver and driver.is_on_demand
    assert driver.flashable("/dev/ttyUSB0", "dev") == []


class ExampleController(AbstractController[Device, Parameter, "ExampleController.Settings"]):
    needs = [Need("example")]

    class Settings(BaseModel):
        channel: int = 11

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return {}

    async def start(self):
        pass

    async def stop(self):
        pass

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


async def test_a_controller_is_a_peripheral_with_its_settings_and_its_peripherals():
    controller = ExampleController(build_test_platform())
    controller.peripherals = Peripherals({"example": Endpoint("example", "echo", "ws://x")})  # library mode
    assert controller.settings.channel == 11  # its defaults until the runtime hands over the saved ones
    assert controller.peripherals.endpoint("example").url == "ws://x"
    assert not controller.is_on_demand and controller.provides == ()
    assert await controller.needed_peripheral_did_change(PeripheralChange("example", None, None)) is False
    assert await controller.context_did_change(Context()) is True  # the context concerns few: it reads it when it does


def test_a_controller_without_settings_takes_none():
    class Plain(ExampleController):
        Settings = NoSettings

    assert Plain(build_test_platform()).settings == NoSettings()
    with pytest.raises(ValidationError):
        NoSettings.model_validate({"channel": 11})
    assert AbstractController.needs == ()
