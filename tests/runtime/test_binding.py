"""How the runtime binds needs: in the order it is given, single-use endpoints once, and why a need stays unbound."""

import json

import pytest
from support import run, settle

from majordom_integration_sdk import Endpoint, Need
from majordom_integration_sdk.capabilities import BLE_ROUTER
from majordom_integration_sdk.runtime import PeripheralState, PeripheralStatus
from majordom_integration_sdk.runtime import StatusKind as K
from majordom_integration_sdk.testing import FakePeripheral

ADAPTERS = [Endpoint("ble", "bluez", "hci0"), Endpoint("ble", "bluez", "hci1")]
ROUTER = Endpoint("ble", "router", "router", is_shared=True, instance=object())


def adapters() -> FakePeripheral:
    return FakePeripheral("bluetooth-adapters", provides=ADAPTERS)


def router() -> FakePeripheral:
    return FakePeripheral(
        "ble-router",
        [Need("ble", ["bluez"], is_many=True, is_required=False)],
        [ROUTER],
        is_on_demand=True,
        answer=True,
    )


async def test_a_many_need_binds_every_free_matching_endpoint(tmp_path):
    sniffer = FakePeripheral("sniffer", [Need("ble", ["bluez"], is_many=True)])
    runtime = await run(tmp_path, adapters(), sniffer)

    assert runtime.bound(sniffer) == {"ble": ADAPTERS}
    assert sniffer.peripherals.endpoints("ble") == ADAPTERS
    with pytest.raises(TypeError, match="endpoints"):
        sniffer.peripherals.endpoint("ble")
    assert runtime.providers(sniffer) == {"ble": ["bluetooth-adapters", "bluetooth-adapters"]}  # one per endpoint


async def test_a_many_need_that_finds_nothing_is_unbound(tmp_path):
    sniffer = FakePeripheral("sniffer", [Need("lora", is_many=True)])
    runtime = await run(tmp_path, sniffer)
    assert runtime.unbound(sniffer) == {"lora": "need lora/*: nothing offers it"}


async def test_an_in_process_provider_is_a_shared_endpoint_carrying_itself(tmp_path):
    ble, matter, homekit = router(), FakePeripheral("matter", [Need("ble", ["router"])]), None
    homekit = FakePeripheral("homekit", [Need("ble", ["router"])])
    runtime = await run(tmp_path, ble, matter, homekit)

    instance = ROUTER.instance
    assert matter.peripherals.instance(BLE_ROUTER) is instance and homekit.peripherals.instance(BLE_ROUTER) is instance
    assert runtime.users(ble) == ["matter", "homekit"]


async def test_the_first_given_takes_every_adapter_and_the_others_hear_what_to_need(tmp_path):
    ble, raw = router(), FakePeripheral("raw", [Need("ble", ["bluez"])])
    matter = FakePeripheral("matter", [Need("ble", ["router"])])
    runtime = await run(tmp_path, adapters(), ble, raw, matter)

    assert ble.peripherals.endpoints("ble") == ADAPTERS
    assert runtime.status(matter).state == PeripheralState.RUNNING  # the others run on
    assert runtime.unbound(raw) == {
        "ble": 'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'
    }


async def test_a_router_nothing_uses_leaves_the_adapters_to_who_needs_them_raw(tmp_path):
    ble, raw = router(), FakePeripheral("raw", [Need("ble", ["bluez"])])
    runtime = await run(tmp_path, adapters(), ble, raw)

    assert runtime.status(ble) == PeripheralStatus(PeripheralState.WAITING, K.UNUSED, "nothing that runs uses it")
    assert runtime.status(raw).state == PeripheralState.RUNNING
    assert raw.peripherals.endpoint("ble") == ADAPTERS[0]


async def test_on_demand_peripherals_binding_only_each_other_dont_run(tmp_path):
    a = FakePeripheral("a", [Need("b", is_required=False)], [Endpoint("a", is_shared=True)], is_on_demand=True)
    b = FakePeripheral("b", [Need("a", is_required=False)], [Endpoint("b", is_shared=True)], is_on_demand=True)
    runtime = await run(tmp_path, a, b)
    assert (
        runtime.status(a)
        == runtime.status(b)
        == PeripheralStatus(PeripheralState.WAITING, K.UNUSED, "nothing that runs uses it")
    )
    assert a.calls == b.calls == []


async def test_a_single_use_endpoint_goes_to_the_first_given_and_then_to_the_next(tmp_path):
    stick = FakePeripheral("stick", provides=[Endpoint("zigbee", "ezsp", "/dev/ttyUSB0")])
    first, second = FakePeripheral("first", [Need("zigbee")]), FakePeripheral("second", [Need("zigbee")])
    runtime = await run(tmp_path, stick, first, second)
    assert runtime.status(first).state == PeripheralState.RUNNING
    assert runtime.status(second) == PeripheralStatus(
        PeripheralState.WAITING, K.NEEDS_UNBOUND, "need zigbee/*: every provider taken by first"
    )

    await runtime.disable(first)
    await settle(runtime)
    assert runtime.status(second).state == PeripheralState.RUNNING


async def test_an_adapter_goes_by_the_order_given_alone(tmp_path):
    (tmp_path / "data").mkdir()
    reserved = {"settings": {}, "reserved": {"matterjs-server": {"ble": "hci1"}}}  # kept by an older runtime
    (tmp_path / "data" / "state.json").write_text(json.dumps(reserved))
    ble = router()
    server = FakePeripheral("matterjs-server", [Need("ble", ["bluez"])], [Endpoint("matter", "x", "ws")])
    runtime = await run(tmp_path, adapters(), ble, FakePeripheral("matter", [Need("ble", ["router"])]), server)

    assert ble.peripherals.endpoints("ble") == ADAPTERS
    assert runtime.unbound(server) == {
        "ble": 'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'
    }
    assert "reserved" not in runtime.state and not hasattr(runtime, "reserve")


async def test_the_first_given_of_two_providers_serves_a_need(tmp_path):
    multipan = FakePeripheral("silabs-multiprotocol", provides=[Endpoint("thread", "otbr-rest", "a", is_shared=True)])
    otbr = FakePeripheral("otbr", provides=[Endpoint("thread", "otbr-rest", "b", is_shared=True)])
    matter = FakePeripheral("matter", [Need("thread")])
    runtime = await run(tmp_path, multipan, otbr, matter)
    assert runtime.providers(matter) == {"thread": "silabs-multiprotocol"}
    assert not hasattr(runtime, "pin")  # the order given and Pinned say what to prefer


async def test_a_provider_that_cant_run_says_why(tmp_path):
    multipan = FakePeripheral(
        "silabs-multiprotocol", [Need("multipan", ["silabs-cpc"])], [Endpoint("zigbee", "ezsp")], is_on_demand=True
    )
    zigbee = FakePeripheral("zigbee", [Need("zigbee", ["ezsp", "znp"])])
    runtime = await run(tmp_path, multipan, zigbee)
    assert runtime.unbound(zigbee) == {
        "zigbee": "need zigbee/ezsp|znp: silabs-multiprotocol can't run (need multipan/silabs-cpc: nothing offers it)"
    }
    assert runtime.unbound(multipan) == {"multipan": "need multipan/silabs-cpc: nothing offers it"}


async def test_a_need_one_that_doesnt_run_could_bind_has_no_reason(tmp_path):
    host = FakePeripheral("host", provides=[Endpoint("ip", is_shared=True)])
    server = FakePeripheral("server", [Need("ip")], [Endpoint("matter")], is_on_demand=True)
    runtime = await run(tmp_path, host, server)
    assert runtime.unbound(server) == {}
    assert runtime.status(server).detail == "nothing that runs uses it"


async def test_what_a_firmware_would_serve_through_what_runs_here(tmp_path):
    multipan = FakePeripheral(
        "silabs-multiprotocol",
        [Need("multipan")],
        [Endpoint("zigbee", "ezsp"), Endpoint("thread", "otbr-rest")],
        is_on_demand=True,
    )
    runtime = await run(tmp_path, multipan, FakePeripheral("zigbee", [Need("zigbee")]))
    assert runtime.reach([Endpoint("multipan", "silabs-cpc")]) == ["zigbee"]  # thread: no one needs it
    assert runtime.reach([Endpoint("zwave")]) == []
