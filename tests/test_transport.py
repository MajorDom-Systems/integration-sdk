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
