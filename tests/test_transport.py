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
    assert await service.provision(Endpoint("matter", "ws", "ws://127.0.0.1:5580/ws"), None) is None
    assert await service.learn(Endpoint("matter", "ws", "ws://127.0.0.1:5580/ws")) == {}


def test_services_name_no_host_ports_by_default():
    assert Service("server", compose={}).ports == {}


def test_endpoint_params_say_how_to_talk_to_it():
    stick = Endpoint("zigbee", "ezsp", "/dev/ttyUSB0", params={"baud": 115200, "flow_control": True})
    assert stick.params["baud"] == 115200
    assert Endpoint("zigbee").params == {}
    assert hash(stick) == hash(Endpoint("zigbee", "ezsp", "/dev/ttyUSB0"))


async def test_without_a_runtime_services_cant_be_restarted():
    from majordom_integration_sdk.testing import build_test_dependencies

    with pytest.raises(NotImplementedError):
        await build_test_dependencies().services.restart("matter-server")
