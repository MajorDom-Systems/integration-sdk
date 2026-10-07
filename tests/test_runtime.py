import json

import pytest
from majordom_integration_sdk import Endpoint, Need, Service, WifiCredentials
from pydantic import BaseModel

from majordom_transports import Runtime

MATTER_SERVER = Service(
    "matter-server",
    compose={"image": "matter", "environment": {"OTBR": "{links.thread.url}", "REGION": "{context.region}"}},
    needs=[Need("ip"), Need("thread", ["otbr-rest"], required=False)],
    provides=[Endpoint("matter", "ws", "ws://127.0.0.1:5580/ws")],
)


class ZigbeeSettings(BaseModel):
    tx_power: int | None = None


class Zigbee:
    needs = [Need("zigbee")]
    services = []
    settings = ZigbeeSettings

    @classmethod
    async def probe(cls, path):
        return Endpoint("zigbee", "znp", path) if path.endswith("ti") else None


class Matter:
    needs = [Need("matter", ["ws"])]
    services = [MATTER_SERVER]
    settings = None

    @classmethod
    async def probe(cls, path):
        return None


@pytest.fixture
def stick(tmp_path):
    """A fake device node; radios are recognised from saved state, so nothing is probed."""

    def make(name, **entry):
        path = tmp_path / name
        path.touch()
        state = tmp_path / "data" / "state.json"
        saved = json.loads(state.read_text()) if state.exists() else {"radios": {}}
        saved["radios"][name] = {"path": str(path), **entry}
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps(saved))
        return str(path)

    return make


def runtime(tmp_path, devices=(), **kwargs):
    return Runtime(
        tmp_path / "data",
        {"zigbee": Zigbee, "matter": Matter},
        docker=False,
        scan=False,
        devices={d.rsplit("/", 1)[1]: d for d in devices},
        **kwargs,
    )


def compose(tmp_path):
    return json.loads((tmp_path / "data" / "compose.json").read_text())["services"]


async def test_multipan_stick_serves_zigbee_and_matter_through_one_service(tmp_path, stick):
    path = stick("sky", driver="silabs", firmware="multipan")
    rt = runtime(tmp_path, [path])
    await rt.refresh()

    assert rt.links("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")}
    assert rt.links("matter")["matter"].url == "ws://127.0.0.1:5580/ws"
    services = compose(tmp_path)
    assert set(services) == {"multiprotocol", "matter-server"}
    assert services["multiprotocol"]["devices"][0] == f"{path}:/dev/ttyACM0"
    assert services["matter-server"]["environment"]["OTBR"] == "http://127.0.0.1:8081"
    assert services["matter-server"]["depends_on"] == ["multiprotocol"]
    assert rt.radios()[0].used_by == ["zigbee", "matter"]


async def test_split_radios_use_direct_zigbee_and_a_standalone_border_router(tmp_path, stick):
    ncp = stick("ncp", driver="silabs", firmware="zigbee")
    rcp = stick("rcp", driver="silabs", firmware="thread")
    rt = runtime(tmp_path, [ncp, rcp])
    await rt.refresh()

    assert rt.links("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", ncp)}
    assert set(compose(tmp_path)) == {"otbr", "matter-server"}
    assert compose(tmp_path)["otbr"]["devices"][0] == f"{rcp}:/dev/ttyRCP"


async def test_without_radios_zigbee_waits_and_matter_runs_over_ip(tmp_path):
    rt = runtime(tmp_path)
    await rt.refresh()

    assert rt.links("zigbee") is None
    assert rt.links("matter") is not None
    assert compose(tmp_path)["matter-server"]["environment"]["OTBR"] == ""


async def test_pinned_endpoint_replaces_the_border_router(tmp_path, stick):
    rcp = stick("rcp", driver="silabs", firmware="thread")
    rt = runtime(tmp_path, [rcp], pinned={"thread": Endpoint("thread", "otbr-rest", "http://mine:8081", shared=True)})
    await rt.refresh()

    assert set(compose(tmp_path)) == {"matter-server"}
    assert compose(tmp_path)["matter-server"]["environment"]["OTBR"] == "http://mine:8081"


async def test_unknown_stick_is_recognised_by_an_integration_probe(tmp_path, monkeypatch):
    from majordom_transports import runtime as module

    monkeypatch.setattr(module, "DRIVERS", {})
    (tmp_path / "ti").touch()
    rt = runtime(tmp_path, [str(tmp_path / "ti")])
    await rt.refresh()

    assert rt.links("zigbee") == {"zigbee": Endpoint("zigbee", "znp", str(tmp_path / "ti"))}


async def test_user_pin_picks_between_two_providers(tmp_path, stick):
    sky = stick("sky", driver="silabs", firmware="multipan")
    rcp = stick("rcp", driver="silabs", firmware="thread")
    rt = runtime(tmp_path, [sky, rcp])
    await rt.refresh()
    assert compose(tmp_path)["matter-server"]["depends_on"] == ["multiprotocol"]

    await rt.pin("thread", "otbr")
    assert compose(tmp_path)["matter-server"]["depends_on"] == ["otbr"]


async def test_settings_persist_and_feed_context(tmp_path):
    rt = runtime(tmp_path, default_wifi=lambda: WifiCredentials("home", "pw"))
    assert rt.settings("zigbee") == ZigbeeSettings()
    assert (await rt.update_settings("zigbee", {"tx_power": -20})).tx_power == -20
    assert runtime(tmp_path).settings("zigbee").tx_power == -20
    assert rt.context().wifi == WifiCredentials("home", "pw")

    wifi = [{"ssid": "iot", "password": "x"}]
    await rt.update_settings("hub", {"region": "Europe", "wifi": wifi, "wifi_ssid": "iot"})
    assert rt.context().region == "Europe"
    assert compose(tmp_path)["matter-server"]["environment"]["REGION"] == "Europe"
    assert rt.context().wifi == WifiCredentials("iot", "x")
    assert rt.settings_scopes("matter") == []


async def test_firmware_options_name_what_each_firmware_would_serve(tmp_path, stick):
    stick("usb-Nabu_Casa_SkyConnect_v1.0_1-if00", driver="silabs", firmware="thread")
    rt = runtime(tmp_path, channel="dev")
    radio = rt.radios()[0]
    assert radio.options == {"zigbee": ["zigbee"], "thread": ["thread"], "multipan": ["thread", "zigbee"]}
    assert runtime(tmp_path).radios()[0].options == {}  # stable channel: nothing verified to flash yet
