"""How the runtime binds needs to its own providers, to what consumers offer, and why a need stays unbound."""

import asyncio
import json
from typing import Any, cast

import pytest

from majordom_integration_sdk import Endpoint, Need, PeripheralChange, Service
from majordom_integration_sdk.runtime import Runtime
from majordom_integration_sdk.runtime.peripherals.providers.ble import cpc_hci
from majordom_integration_sdk.testing import FakeBLERouter, FakeBLESource, FakeSSDP

ADAPTERS = [Endpoint("ble", "bluez", "hci0"), Endpoint("ble", "bluez", "hci1")]


def consumer(*needs: Need, services: list[Service] | None = None) -> Any:
    return type("Consumer", (), {"needs": list(needs), "services": services or [], "settings": None})


class RecordingRouter(FakeBLERouter):
    """The router, keeping what the runtime told it."""

    def __init__(self):
        super().__init__()
        self.changes: list[PeripheralChange] = []

    async def peripheral_did_change(self, change: PeripheralChange):
        self.changes.append(change)


@pytest.fixture(autouse=True)
def adapters(monkeypatch):
    monkeypatch.setattr("majordom_integration_sdk.runtime.runtime._host_endpoints", lambda: list(ADAPTERS))


def runtime(tmp_path, consumers: dict[str, Any], *builtins, **kwargs) -> Runtime:
    return Runtime(tmp_path / "data", consumers, docker=False, scan=False, builtins=list(builtins), **kwargs)


async def test_a_many_need_binds_every_free_matching_endpoint(tmp_path):
    rt = runtime(tmp_path, {"sniffer": consumer(Need("ble", ["bluez"], is_many=True))})
    await rt.refresh()

    peripherals = rt.peripherals("sniffer")
    assert peripherals is not None
    assert peripherals["ble"] == ADAPTERS
    assert peripherals.endpoints("ble") == ADAPTERS
    with pytest.raises(TypeError, match="endpoints"):
        peripherals.endpoint("ble")
    assert rt.providers("sniffer") == {"ble": "host, host"}


async def test_a_many_need_that_finds_nothing_is_unbound(tmp_path):
    rt = runtime(tmp_path, {"sniffer": consumer(Need("lora", is_many=True))})
    await rt.refresh()
    assert rt.peripherals("sniffer") is None
    assert rt.unbound("sniffer") == {"lora": "need lora/*: nothing offers it"}


async def test_in_process_providers_are_shared_endpoints_carrying_themselves(tmp_path):
    router, ssdp = FakeBLERouter(), FakeSSDP()
    consumers = {"matter": consumer(Need("ble", ["router"])), "hue": consumer(Need("upnp", ["ssdp"]))}
    rt = runtime(tmp_path, consumers, router, ssdp)
    await rt.refresh()

    matter, hue = rt.peripherals("matter"), rt.peripherals("hue")
    assert matter and hue
    assert matter.instance("ble") is router
    assert hue.instance("upnp") is ssdp
    assert matter.endpoint("ble") == Endpoint("ble", "router", "router", is_shared=True)


async def test_the_router_binds_first_and_takes_every_adapter(tmp_path):
    router = RecordingRouter()
    raw = consumer(Need("ble", ["bluez"]))
    rt = runtime(tmp_path, {"raw": raw, "matter": consumer(Need("ble", ["router"]))}, router)
    await rt.refresh()

    assert router.changes == [PeripheralChange("ble", None, ADAPTERS)]
    assert rt.peripherals("matter") is not None  # the others run on
    assert rt.peripherals("raw") is None  # an ordinary unmet need, for that consumer only
    assert rt.unbound("raw") == {
        "ble": 'need ble/bluez: every provider taken by ble-router — use Need("ble", ["router"])'
    }


async def test_a_service_that_needs_raw_bluetooth_fails_alone_with_its_dependants(tmp_path):
    direct = Service(
        "ble-server", compose={"image": "x"}, needs=[Need("ble", ["bluez"])], provides=[Endpoint("ble-api")]
    )
    consumers = {"app": consumer(Need("ble-api"), services=[direct]), "matter": consumer(Need("ble", ["router"]))}
    rt = runtime(tmp_path, consumers, FakeBLERouter())
    await rt.refresh()

    assert rt.peripherals("app") is None
    assert "ble-server can't run (need ble/bluez: every provider taken by ble-router" in rt.unbound("app")["ble-api"]
    assert rt.peripherals("matter") is not None


async def test_a_reserved_adapter_goes_to_its_owner_before_the_router(tmp_path):
    router = RecordingRouter()
    server = Service("matterjs-server", {"image": "matter"}, [Need("ble", ["bluez"])], [Endpoint("matter", "x", "ws")])
    rt = runtime(tmp_path, {"matter": consumer(Need("matter"), services=[server])}, router)
    await rt.reserve("matterjs-server", "ble", "hci1")

    assert router.changes[-1].after == [ADAPTERS[0]]  # the router skips it
    assert rt.peripherals("matter") is not None
    assert rt.providers("matterjs-server") == {"ble": "host"}
    assert json.loads((tmp_path / "data" / "compose.json").read_text())["services"]["matterjs-server"]

    await rt.reserve("matterjs-server", "ble", None)
    assert router.changes[-1].after == ADAPTERS


async def test_a_reserved_endpoint_is_no_one_elses(tmp_path):
    rt = runtime(tmp_path, {"raw": consumer(Need("ble", ["bluez"], is_many=True))})
    await rt.reserve("someone", "ble", "hci0")
    assert (rt.peripherals("raw") or {}).get("ble") == [ADAPTERS[1]]


async def test_offers_bind_as_they_come_and_go_without_rebinding_the_offerer(tmp_path):
    router = RecordingRouter()
    changes: list[Any] = []

    async def on_change():
        changes.append(rt.peripherals("esphome"))

    esphome = consumer(Need("ble", ["router"], is_required=False))
    rt = runtime(tmp_path, {"esphome": esphome}, router, on_change=on_change)
    await rt.refresh()
    before = rt.peripherals("esphome")
    proxy = FakeBLESource("esphome:aa")
    lent = Endpoint("ble", "esphome-proxy", "esphome:aa", instance=proxy)

    await rt.platform("esphome", _platform(tmp_path)).offers.offer(lent)
    assert router.changes[-1] == PeripheralChange("ble", ADAPTERS, [*ADAPTERS, lent])
    assert cast(list[Endpoint], router.changes[-1].after)[-1].instance is proxy
    assert rt.providers("ble-router") == {"ble": "host, host, esphome"}
    assert rt.peripherals("esphome") == before  # it never binds what it offers itself
    await asyncio.sleep(0)  # the hub hears about it after the offer returned
    assert changes == [before, before]

    await rt.withdraw("esphome", lent)
    assert router.changes[-1] == PeripheralChange("ble", [*ADAPTERS, lent], ADAPTERS)


async def test_a_radio_whose_firmware_serves_bluetooth_offers_it_too(tmp_path, monkeypatch):
    path = tmp_path / "sky"
    path.touch()
    asked: list[str] = []

    async def has_bluetooth(at: str) -> bool:
        asked.append(at)
        return True

    monkeypatch.setattr(cpc_hci, "has_bluetooth", has_bluetooth)
    state = {"radios": {"sky": {"path": str(path), "driver": "silabs", "firmware": "multipan-rcp"}}}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps(state))
    router = RecordingRouter()
    rt = Runtime(tmp_path / "data", {}, docker=False, scan=False, devices={"sky": str(path)}, builtins=[router])
    await rt.refresh()
    await rt.refresh()

    assert asked == [str(path)]  # asked once, then remembered
    cpc = Endpoint("ble", "silabs-cpc-hci", str(path), params={"hci": None})
    assert router.changes[-1].after == [*ADAPTERS, cpc]


async def test_a_radio_without_bluetooth_offers_none(tmp_path):
    path = tmp_path / "zb"
    path.touch()
    state = {"radios": {"zb": {"path": str(path), "driver": "silabs", "firmware": "zigbee-ncp"}}}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps(state))
    router = RecordingRouter()
    rt = Runtime(tmp_path / "data", {}, docker=False, scan=False, devices={"zb": str(path)}, builtins=[router])
    await rt.refresh()
    assert router.changes[-1].after == ADAPTERS
    assert "bluetooth" not in rt.state["radios"]["zb"]  # not a multi-PAN firmware: nothing to ask


async def test_the_cpc_probe_says_no_until_verified_on_hardware():
    assert await cpc_hci.has_bluetooth("/dev/ttyUSB0") is False
    with pytest.raises(ConnectionError, match="no Bluetooth adapter"):
        cpc_hci.source_of(Endpoint("ble", "silabs-cpc-hci", "/dev/ttyUSB0", params={"hci": None}))


async def test_providers_start_and_stop_with_the_runtime(tmp_path):
    started: list[str] = []

    class Provider(FakeSSDP):
        async def start(self):
            started.append("start")

        async def stop(self):
            started.append("stop")

    rt = runtime(tmp_path, {}, Provider())
    await rt.start()
    await rt.stop()
    assert started == ["start", "stop"]


def _platform(tmp_path):
    from majordom_integration_sdk.testing import build_test_platform

    return build_test_platform(tmp_path / "docs")


async def test_a_service_hears_whether_it_got_an_adapter(tmp_path):
    server = Service(
        "matterjs-server",
        {
            "image": "m",
            "environment": {
                "BLE_PROXY": "{peripherals.ble.url:!true}",
                "ADAPTER": "{peripherals.ble.url}",
                "BACKEND": "{peripherals.ble.url:?dbus}",
            },
            "volumes": ["/data:/data", "{peripherals.ble.url:?/run/dbus:/run/dbus:ro}"],
        },
        [Need("ble", ["bluez"], is_required=False)],
        [Endpoint("matter", "matter-server", "ws")],
    )
    rt = runtime(tmp_path, {"matter": consumer(Need("matter"), services=[server])}, FakeBLERouter())
    await rt.refresh()

    def service() -> dict[str, Any]:
        return json.loads((tmp_path / "data" / "compose.json").read_text())["services"]["matterjs-server"]

    # the router has every adapter: lend it over /ble, and keep the system bus out
    assert service()["environment"] == {"BLE_PROXY": "true", "ADAPTER": "", "BACKEND": ""}
    assert service()["volumes"] == ["/data:/data"]

    await rt.reserve("matterjs-server", "ble", "hci1")
    assert service()["environment"] == {"BLE_PROXY": "", "ADAPTER": "hci1", "BACKEND": "dbus"}
    assert service()["volumes"] == ["/data:/data", "/run/dbus:/run/dbus:ro"]


async def test_a_consumer_hears_what_its_service_got_through_the_params_it_provides(tmp_path):
    server = Service(
        "matterjs-server",
        {"image": "m"},
        [Need("ble", ["bluez"], is_required=False)],
        [Endpoint("matter", "matter-server", "ws", params={"adapter": "{peripherals.ble.url}", "baud": 9600})],
    )
    rt = runtime(tmp_path, {"matter": consumer(Need("matter"), services=[server])}, FakeBLERouter())
    await rt.refresh()

    def matter() -> Endpoint:
        peripherals = rt.peripherals("matter")
        assert peripherals is not None
        return peripherals.endpoint("matter")

    assert matter().params == {"adapter": "", "baud": 9600}  # the router has every adapter
    await rt.reserve("matterjs-server", "ble", "hci1")
    assert matter().params == {"adapter": "hci1", "baud": 9600}
    assert server.provides[0].params["adapter"] == "{peripherals.ble.url}"  # the template stays for the next binding


def test_the_host_offers_every_adapter_with_its_index(monkeypatch):
    from majordom_integration_sdk.runtime import runtime as module

    monkeypatch.undo()  # the real one, over a fake /sys
    monkeypatch.setattr(
        module,
        "glob",
        lambda pattern: ["/sys/class/bluetooth/hci1", "/sys/class/bluetooth/hci0", "/sys/class/bluetooth/hci0:1"],
    )
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module._host_endpoints() == [
        Endpoint("ip", is_shared=True),
        Endpoint("ble", "bluez", "hci0", params={"index": "0"}),
        Endpoint("ble", "bluez", "hci1", params={"index": "1"}),
    ]


async def test_a_consumer_restarts_only_the_services_it_brings(tmp_path, monkeypatch):
    own = Service("radio-server", {"image": "r"})
    rt = runtime(tmp_path, {"radio": consumer(services=[own])})
    restarted: list[str] = []

    async def restart(service: str):
        restarted.append(service)

    monkeypatch.setattr(rt, "restart", restart)
    services = rt.platform("radio", _platform(tmp_path)).services
    await services.restart("radio-server")
    with pytest.raises(PermissionError):
        await services.restart("silabs-multiprotocol")  # shared, not its own
    assert restarted == ["radio-server"]
