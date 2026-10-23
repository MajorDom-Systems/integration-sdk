import pytest

from majordom_integration_sdk import AbstractController, Endpoint, Need, Service


def test_need_accepts_family_and_dialect():
    assert Need("zigbee").accepts(Endpoint("zigbee", "znp"))
    assert Need("zigbee", ["ezsp", "znp"]).accepts(Endpoint("zigbee", "znp"))
    assert not Need("zigbee", ["ezsp"]).accepts(Endpoint("zigbee", "znp"))
    assert not Need("thread").accepts(Endpoint("zigbee", "ezsp"))


def test_controller_declarations_default_to_nothing():
    assert AbstractController.needs == ()
    assert AbstractController.services == ()
    assert AbstractController.settings is None


async def test_service_hooks_default_to_nothing():
    service = Service("server", compose={})
    assert await service.provision(Endpoint("matter", "matter-server", "ws://127.0.0.1:5580"), None) is None
    assert await service.learn(Endpoint("matter", "matter-server", "ws://127.0.0.1:5580")) == {}


def test_services_name_no_host_ports_by_default():
    assert Service("server", compose={}).ports == {}


def test_endpoint_params_say_how_to_talk_to_it():
    stick = Endpoint("zigbee", "ezsp", "/dev/ttyUSB0", params={"baud": 115200, "flow_control": True})
    assert stick.params["baud"] == 115200
    assert Endpoint("zigbee").params == {}
    assert hash(stick) == hash(Endpoint("zigbee", "ezsp", "/dev/ttyUSB0"))


async def test_without_a_runtime_services_cant_be_restarted():
    from majordom_integration_sdk.testing import build_test_platform

    with pytest.raises(NotImplementedError):
        await build_test_platform().services.restart("matterjs-server")


def test_peripherals_hand_out_one_endpoint_many_or_an_instance():
    from majordom_integration_sdk import Peripherals

    router = object()
    adapters = [Endpoint("ble", "bluez", "hci0"), Endpoint("ble", "bluez", "hci1")]
    peripherals = Peripherals({"zigbee": Endpoint("zigbee", "ezsp", "/dev/ttyUSB0"), "raw": adapters})
    assert peripherals.endpoint("zigbee").url == "/dev/ttyUSB0"
    assert peripherals.endpoints("zigbee") == [peripherals["zigbee"]]
    assert peripherals.endpoints("raw") == adapters
    assert peripherals.endpoints("thread") == []
    assert peripherals.instance("ble") is None
    with pytest.raises(KeyError):
        peripherals.endpoint("thread")

    peripherals["ble"] = Endpoint("ble", "router", "router", shared=True, instance=router)
    assert peripherals.instance("ble") is router


def test_an_endpoint_is_the_same_whatever_instance_it_carries():
    assert Endpoint("ble", "esphome-proxy", "esphome:aa", instance=object()) == Endpoint(
        "ble", "esphome-proxy", "esphome:aa", instance=object()
    )
    assert not Need("ble").many
