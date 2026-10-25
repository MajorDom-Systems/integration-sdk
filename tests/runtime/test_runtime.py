import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from majordom_integration_sdk import Endpoint, Need, Service, WifiCredentials
from majordom_integration_sdk.runtime import Runtime

MATTERJS_SERVER = Service(
    "matterjs-server",
    compose={"image": "matter", "environment": {"OTBR": "{peripherals.thread.url}", "REGION": "{context.region}"}},
    needs=[Need("ip"), Need("thread", ["otbr-rest"], is_required=False)],
    provides=[Endpoint("matter", "matter-server-ws", "ws://127.0.0.1:5580/ws")],
)


NCP = {"baud": 115200, "flow_control": True}  # the firmware's serial settings


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
    needs = [Need("matter", ["matter-server-ws"])]
    services = [MATTERJS_SERVER]
    settings = None

    @classmethod
    async def probe(cls, path):
        return None


@pytest.fixture(autouse=True)
def listening(monkeypatch):
    """Host ports something else listens on; none unless a test says so."""
    ports: set[int] = set()
    monkeypatch.setattr("majordom_integration_sdk.runtime.runtime._listening", lambda port: port in ports)
    return ports


@pytest.fixture(autouse=True)
def held(monkeypatch):
    """Other programs holding a stick open, by its file name; none unless a test says so."""
    holders: dict[str, str] = {}
    monkeypatch.setattr(
        "majordom_integration_sdk.runtime.runtime._holder", lambda path, open_it=False: holders.get(Path(path).name)
    )
    return holders


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
        kwargs.pop("consumers", {"zigbee": Zigbee, "matter": Matter}),
        docker=False,
        scan=False,
        devices={d.rsplit("/", 1)[1]: d for d in devices},
        **{"builtins": [], **kwargs},
    )


def compose(tmp_path):
    return json.loads((tmp_path / "data" / "compose.json").read_text())["services"]


async def test_multipan_stick_serves_zigbee_and_matter_through_one_service(tmp_path, stick):
    path = stick("sky", driver="silabs", firmware="multipan-rcp")
    rt = runtime(tmp_path, [path])
    await rt.refresh()

    assert rt.peripherals("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", "socket://127.0.0.1:9999")}
    assert rt.peripherals("matter")["matter"].url == "ws://127.0.0.1:5580/ws"
    services = compose(tmp_path)
    assert set(services) == {"silabs-multiprotocol", "matterjs-server"}
    assert services["silabs-multiprotocol"]["devices"][0] == f"{path}:/dev/ttyRCP"
    assert services["matterjs-server"]["environment"]["OTBR"] == "http://127.0.0.1:8081"
    assert "depends_on" not in services["matterjs-server"]  # its border router coming and going doesn't restart it
    assert rt.radios()[0].used_by == ["zigbee", "matter"]
    assert rt.radios()[0].offers == ["thread", "zigbee"]


async def test_split_radios_use_direct_zigbee_and_a_standalone_border_router(tmp_path, stick):
    ncp = stick("ncp", driver="silabs", firmware="zigbee-ncp")
    rcp = stick("rcp", driver="silabs", firmware="thread-rcp")
    rt = runtime(tmp_path, [ncp, rcp])
    await rt.refresh()

    assert rt.peripherals("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", ncp, params=NCP)}
    assert set(compose(tmp_path)) == {"otbr", "matterjs-server"}
    assert compose(tmp_path)["otbr"]["devices"][0] == f"{rcp}:/dev/ttyRCP"
    rcp_url = "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate=460800&uart-flow-control"
    assert compose(tmp_path)["otbr"]["environment"]["OT_RCP_DEVICE"] == rcp_url


async def test_serial_settings_come_from_the_firmware_or_the_board(tmp_path, stick):
    rt = runtime(tmp_path, [stick("sky", driver="silabs", firmware="multipan-rcp")])
    await rt.refresh()
    environment = compose(tmp_path)["silabs-multiprotocol"]["environment"]
    assert (environment["RADIO_BAUD"], environment["RADIO_FLOW_CONTROL"]) == ("460800", "True")

    (tmp_path / "ttyAMA3").touch()  # soldered: its board wires the UART without flow control
    board = {"id": "mg26", "path": str(tmp_path / "ttyAMA3"), "driver": "silabs", "firmware": "thread-rcp"}
    rt = runtime(tmp_path / "board", board=[board | {"params": {"baud": 115200, "flow_control": False}}])
    await rt.refresh()
    rcp_url = "spinel+hdlc+uart:///dev/ttyRCP?uart-baudrate=115200"
    assert compose(tmp_path / "board")["otbr"]["environment"]["OT_RCP_DEVICE"] == rcp_url


async def test_without_radios_zigbee_waits_and_matter_runs_over_ip(tmp_path):
    rt = runtime(tmp_path)
    await rt.refresh()

    assert rt.peripherals("zigbee") is None
    assert rt.peripherals("matter") is not None
    assert compose(tmp_path)["matterjs-server"]["environment"]["OTBR"] == ""


async def test_pinned_endpoint_replaces_the_border_router(tmp_path, stick):
    rcp = stick("rcp", driver="silabs", firmware="thread-rcp")
    rt = runtime(
        tmp_path, [rcp], pinned={"thread": Endpoint("thread", "otbr-rest", "http://mine:8081", is_shared=True)}
    )
    await rt.refresh()

    assert set(compose(tmp_path)) == {"matterjs-server"}
    assert compose(tmp_path)["matterjs-server"]["environment"]["OTBR"] == "http://mine:8081"


async def test_unknown_stick_is_recognised_by_an_integration_probe(tmp_path, monkeypatch):
    from majordom_integration_sdk.runtime import runtime as module

    monkeypatch.setattr(module, "DRIVERS", {})
    (tmp_path / "ti").touch()
    rt = runtime(tmp_path, [str(tmp_path / "ti")])
    await rt.refresh()

    assert rt.peripherals("zigbee") == {"zigbee": Endpoint("zigbee", "znp", str(tmp_path / "ti"))}


async def test_user_pin_picks_between_two_providers(tmp_path, stick):
    sky = stick("sky", driver="silabs", firmware="multipan-rcp")
    rcp = stick("rcp", driver="silabs", firmware="thread-rcp")
    rt = runtime(tmp_path, [sky, rcp])
    await rt.refresh()
    assert rt.providers("matterjs-server")["thread"] == "silabs-multiprotocol"

    await rt.pin("thread", "otbr")
    assert rt.providers("matterjs-server")["thread"] == "otbr"


async def test_settings_persist_and_feed_context(tmp_path):
    rt = runtime(tmp_path, default_wifi=lambda: WifiCredentials("home", "pw"))
    assert rt.settings("zigbee") == ZigbeeSettings()
    assert (await rt.update_settings("zigbee", {"tx_power": -20})).tx_power == -20
    assert runtime(tmp_path).settings("zigbee").tx_power == -20
    assert rt.context().wifi == WifiCredentials("home", "pw")

    wifi = [{"ssid": "iot", "password": "x"}]
    await rt.update_settings("hub", {"region": "Europe", "wifi": wifi, "wifi_ssid": "iot"})
    assert rt.context().region == "Europe"
    assert compose(tmp_path)["matterjs-server"]["environment"]["REGION"] == "Europe"
    assert rt.context().wifi == WifiCredentials("iot", "x")
    assert rt.settings_scopes("matter") == []


async def test_a_service_setting_recreates_only_that_service(tmp_path, stick, monkeypatch):
    """Integrations keep running: what they are handed doesn't change, only the service's container does."""
    changes = []

    async def changed():
        changes.append({c: (rt.peripherals(c), rt.settings(c)) for c in rt.consumers} | {"context": rt.context()})

    async def dataset(url):
        return "0e08"

    monkeypatch.setattr("majordom_integration_sdk.runtime.peripherals.services.builtin.active_dataset", dataset)
    rt = runtime(tmp_path, [stick("sky", driver="silabs", firmware="multipan-rcp")], on_change=changed)
    await rt.refresh()
    await settle(rt)
    before = compose(tmp_path)

    await rt.update_settings("silabs-multiprotocol", {"tx_power": -10})
    await settle(rt)
    after = compose(tmp_path)
    assert {name for name in before if before[name] != after[name]} == {"silabs-multiprotocol"}
    assert changes[-1] == changes[-2]  # nothing an integration sees changed: none restarts


async def test_firmware_options_name_what_each_firmware_would_serve(tmp_path, stick):
    stick("usb-Nabu_Casa_SkyConnect_v1.0_1-if00", driver="silabs", firmware="thread-rcp")
    rt = runtime(tmp_path)
    assert rt.radios()[0].options == {}  # stable channel: nothing verified to flash yet
    await rt.update_settings("hub", {"firmware": "dev"})
    assert rt.radios()[0].options == {
        "zigbee-ncp": ["zigbee"],
        "thread-rcp": ["thread"],
        "multipan-rcp": ["thread", "zigbee"],
    }


async def test_services_get_free_host_ports_and_keep_them(tmp_path, stick, listening):
    sky = stick("sky", driver="silabs", firmware="multipan-rcp")
    rt = runtime(tmp_path, [sky])
    await rt.refresh()
    assert rt.peripherals("zigbee")["zigbee"].url == "socket://127.0.0.1:9999"  # the preferred port, free
    assert compose(tmp_path)["silabs-multiprotocol"]["environment"]["EZSP_TCP_PORT"] == "9999"

    listening.add(9999)  # after a reboot something else took it before our container came up
    rt = runtime(tmp_path, [sky])
    await rt.refresh()
    moved = rt.peripherals("zigbee")["zigbee"].url
    assert moved != "socket://127.0.0.1:9999"
    assert compose(tmp_path)["silabs-multiprotocol"]["environment"]["EZSP_TCP_PORT"] == moved.rsplit(":", 1)[1]

    listening.clear()  # the preferred port is free again: the service stays where it is now
    rt = runtime(tmp_path, [sky])
    await rt.refresh()
    assert rt.peripherals("zigbee")["zigbee"].url == moved


async def test_a_port_held_by_our_own_running_container_is_kept(tmp_path, stick, listening, monkeypatch):
    sky = stick("sky", driver="silabs", firmware="multipan-rcp")
    rt = runtime(tmp_path, [sky])
    await rt.refresh()
    listening.update({9999, 8081})  # our silabs-multiprotocol container is up and holds them

    async def running(self):
        return {"silabs-multiprotocol"}

    monkeypatch.setattr(Runtime, "_running", running)
    rt = runtime(tmp_path, [sky])
    await rt.refresh()
    assert rt.peripherals("zigbee")["zigbee"].url == "socket://127.0.0.1:9999"


async def test_services_preferring_the_same_port_get_different_ones(tmp_path, stick):
    bridge = Service(
        "bridge",
        {"image": "bridge", "command": ["--port", "{ports.api}"]},
        provides=[Endpoint("matter", "matter-server-ws", "ws://127.0.0.1:{ports.api}/ws")],
        ports={"api": 9999},
    )

    class Bridged(Matter):
        services = [bridge]

    consumers: dict[str, Any] = {"zigbee": Zigbee, "matter": Bridged}
    path = stick("sky", driver="silabs", firmware="multipan-rcp")
    rt = Runtime(tmp_path / "data", consumers, docker=False, scan=False, devices={"sky": path})
    await rt.refresh()
    zigbee, matter = rt.peripherals("zigbee"), rt.peripherals("matter")
    assert zigbee and matter
    api = compose(tmp_path)["bridge"]["command"][1]
    assert zigbee.endpoint("zigbee").url.rsplit(":", 1)[1] != api
    assert matter.endpoint("matter").url == f"ws://127.0.0.1:{api}/ws"


async def settle(rt):
    """Let what services set up and learn finish."""
    await asyncio.gather(*(task for tasks in rt._tasks.values() for task in tasks))


async def test_services_set_up_what_the_runtime_runs_and_teach_it_facts(tmp_path):
    calls = []

    class Router(Service):
        async def provision(self, endpoint, settings):
            calls.append(("provision", endpoint.url))

        async def learn(self, endpoint):
            calls.append(("learn", endpoint.url))
            return {"thread_dataset": "0e08"}

    class Thread:
        needs = [Need("thread")]
        services = [Router("router", {"image": "router"}, [Need("ip")], [Endpoint("thread", "rest", "http://ours")])]
        settings = None

    consumers: dict[str, Any] = {"thread": Thread}
    rt = Runtime(tmp_path / "ours", consumers, docker=False, scan=False)
    await rt.refresh()
    await settle(rt)
    assert sorted(calls) == [("learn", "http://ours"), ("provision", "http://ours")]
    assert rt.context().thread_dataset == "0e08"

    calls.clear()
    pinned = {"thread": Endpoint("thread", "rest", "http://theirs")}
    rt = Runtime(tmp_path / "theirs", consumers, docker=False, scan=False, pinned=pinned)
    await rt.refresh()
    await settle(rt)
    assert calls == [("learn", "http://theirs")]  # someone else's router is read, never set up
    assert rt.context().thread_dataset == "0e08"


async def test_a_refresh_that_changes_nothing_lets_setup_finish(tmp_path):
    """A border router still forming its network when a refresh came was cancelled and never set up."""
    release, provisioned = asyncio.Event(), []

    class Router(Service):
        async def provision(self, endpoint, settings):
            await release.wait()
            provisioned.append(endpoint.url)

        async def learn(self, endpoint):
            return {}

    class Thread:
        needs = [Need("thread")]
        services = [Router("router", {"image": "router"}, [Need("ip")], [Endpoint("thread", "rest", "http://ours")])]
        settings = None

    rt = Runtime(tmp_path / "data", {"thread": Thread}, docker=False, scan=False)
    await rt.refresh()
    await asyncio.sleep(0)
    await rt.refresh()  # recreates nothing
    release.set()
    await settle(rt)
    assert provisioned == ["http://ours"]


async def test_recreating_services_keeps_the_thread_network_known(tmp_path, stick, monkeypatch):
    """A service recreated behind the same border router keeps the Thread network in the context:
    dropping it meanwhile restarted every integration twice."""
    reads, seen = [], []

    async def dataset(url):
        reads.append(url)
        return "0e08"

    async def changed():
        seen.append(rt.context().thread_dataset)

    monkeypatch.setattr("majordom_integration_sdk.runtime.peripherals.services.builtin.active_dataset", dataset)
    rt = runtime(tmp_path, [stick("sky", driver="silabs", firmware="multipan-rcp")], on_change=changed)
    await rt.refresh()
    await settle(rt)
    await rt.update_settings("hub", {"region": "Europe"})  # recreates matter-server
    await settle(rt)
    assert len(reads) == 2  # read again: a recreated border router may hold another network
    assert seen == [None, "0e08", "0e08"]


async def test_only_known_radios_are_opened_until_asked(tmp_path, monkeypatch):
    from majordom_integration_sdk.runtime import runtime as module

    usb = {"sky": ("10c4", "ea60"), "arduino": ("1a86", "7523")}  # an Arduino resets whenever its port opens
    opened = []

    class Silabs(module.DRIVERS["silabs"].__class__):
        async def identify(self, path):
            opened.append(path.rsplit("/", 1)[1])
            return "multipan-rcp" if path.endswith("sky") else None

    monkeypatch.setattr(module, "DRIVERS", {"silabs": Silabs()})
    monkeypatch.setattr(module, "glob", lambda pattern: [f"/dev/serial/by-id/{name}" for name in usb])
    monkeypatch.setattr(module, "_usb_id", lambda path: usb[path.rsplit("/", 1)[1]])
    rt = Runtime(tmp_path / "data", {"zigbee": Zigbee}, docker=False)

    await rt.refresh()
    assert opened == ["sky"]
    assert {r.id: r.driver for r in rt.radios()} == {"sky": "silabs", "arduino": None}

    await rt.probe("arduino")  # the user asked: open it after all
    assert opened == ["sky", "arduino"]


async def test_a_stick_another_program_holds_is_left_alone_until_claimed(tmp_path, monkeypatch, held):
    from majordom_integration_sdk.runtime import runtime as module
    from majordom_integration_sdk.runtime.peripherals.hardware import drivers

    opened = []

    class Silabs(drivers.Silabs):
        async def identify(self, path):
            opened.append(path)
            return "zigbee-ncp"

    monkeypatch.setattr(module, "DRIVERS", {"silabs": Silabs()})
    monkeypatch.setattr(module, "glob", lambda pattern: ["/dev/serial/by-id/sky"] if "serial" in pattern else [])
    monkeypatch.setattr(module, "_usb_id", lambda path: ("10c4", "ea60"))
    held["sky"] = "zigbee2mqtt"
    consumers: dict[str, Any] = {"zigbee": Zigbee}
    rt = Runtime(tmp_path / "data", consumers, docker=False)

    await rt.refresh()
    assert opened == []  # never opened: probing would reset the radio under zigbee2mqtt
    assert [(r.id, r.held_by, r.driver) for r in rt.radios()] == [("sky", "zigbee2mqtt", None)]

    await rt.probe("sky")  # the user claims it anyway
    assert opened == ["/dev/serial/by-id/sky"]
    assert rt.peripherals("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", "/dev/serial/by-id/sky", params=NCP)}
    assert rt.radios()[0].held_by is None


async def test_a_known_stick_another_program_took_is_not_bound(tmp_path, stick, held):
    path = stick("ncp", driver="silabs", firmware="zigbee-ncp")
    held["ncp"] = "node"
    rt = runtime(tmp_path, [path])
    await rt.refresh()
    assert rt.peripherals("zigbee") is None
    assert rt.radios()[0].held_by == "node"

    await rt.pin("zigbee", "ncp")  # choosing it for Zigbee claims it
    assert rt.peripherals("zigbee") == {"zigbee": Endpoint("zigbee", "ezsp", path, params=NCP)}


async def test_a_stick_is_used_once_the_other_program_lets_go(tmp_path, stick, held):
    path = stick("ncp", driver="silabs", firmware="zigbee-ncp")
    held["ncp"] = "node"
    rt = runtime(tmp_path, [path], poll=0.01)
    await rt.start()
    assert rt.peripherals("zigbee") is None

    held.clear()
    async with asyncio.timeout(2):
        while rt.peripherals("zigbee") is None:
            await asyncio.sleep(0.01)
    await rt.stop()


async def test_a_radio_is_released_before_it_is_probed_again(tmp_path, stick, monkeypatch):
    from majordom_integration_sdk.runtime import runtime as module

    path = stick("sky", driver="silabs", firmware="multipan-rcp")
    running_while_probed = []

    class Silabs(module.DRIVERS["silabs"].__class__):
        async def identify(self, path):
            running_while_probed.append(set(compose(tmp_path)))
            return "multipan-rcp"

    monkeypatch.setattr(module, "DRIVERS", {"silabs": Silabs()})
    rt = runtime(tmp_path, [path])
    await rt.refresh()
    assert "silabs-multiprotocol" in compose(tmp_path)

    await rt.forget("sky")  # plugged in: identified again, after the multi-PAN service let go of the stick
    assert running_while_probed == [{"matterjs-server"}]
    assert "silabs-multiprotocol" in compose(tmp_path)


async def test_a_probe_that_never_answers_gives_up(tmp_path, monkeypatch):
    from majordom_integration_sdk.runtime import runtime as module

    class Silent(module.DRIVERS["silabs"].__class__):
        async def identify(self, path):
            await asyncio.Event().wait()  # e.g. bellows resetting a port someone else holds

    monkeypatch.setattr(module, "DRIVERS", {"silabs": Silent()})
    monkeypatch.setattr(module, "PROBE_TIMEOUT", 0.01)
    (tmp_path / "odd").touch()
    rt = runtime(tmp_path, [str(tmp_path / "odd")])
    async with asyncio.timeout(1):
        await rt.refresh()
    assert rt.radios()[0].driver is None


async def test_a_failed_compose_up_is_retried_on_the_next_refresh(tmp_path, stick, monkeypatch):
    calls = []

    class Process:
        returncode = 1

        async def communicate(self):
            return b"Conflict. The container name is already in use", b""

    async def docker(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", docker)
    rt = runtime(tmp_path, [stick("sky", driver="silabs", firmware="multipan-rcp")])
    rt.docker = True

    await rt.refresh()
    Process.returncode = 0
    await rt.refresh()
    await rt.refresh()
    assert len(calls) == 2  # retried once after the failure, then nothing left to do


async def test_a_stick_that_comes_back_on_another_tty_is_followed(tmp_path):
    """A USB bus reset re-enumerates sticks onto new ttys under the same by-id name, faster than the
    watcher's debounce: services must not keep the old device node (it may be another device now)."""
    (tmp_path / "ttyUSB0").touch()
    (tmp_path / "ttyUSB4").touch()
    link = tmp_path / "usb-SkyConnect"
    link.symlink_to(tmp_path / "ttyUSB0")
    state = tmp_path / "data" / "state.json"
    state.parent.mkdir()
    entry = {"path": str(link), "driver": "silabs", "firmware": "multipan-rcp"}
    state.write_text(json.dumps({"radios": {link.name: entry}}))
    rt = runtime(tmp_path, [str(link)], poll=0.01)
    await rt.start()
    assert compose(tmp_path)["silabs-multiprotocol"]["devices"][0].startswith(f"{(tmp_path / 'ttyUSB0').resolve()}:")

    link.unlink()
    link.symlink_to(tmp_path / "ttyUSB4")
    async with asyncio.timeout(2):
        while not compose(tmp_path)["silabs-multiprotocol"]["devices"][0].startswith(
            f"{(tmp_path / 'ttyUSB4').resolve()}:"
        ):
            await asyncio.sleep(0.01)
    await rt.stop()


async def test_restarting_a_service_restarts_only_its_container(tmp_path, stick, monkeypatch):
    calls, changes = [], []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def docker(*args, **kwargs):
        calls.append(args)
        return Process()

    async def changed():
        changes.append(1)

    rt = runtime(tmp_path, [stick("sky", driver="silabs", firmware="multipan-rcp")], on_change=changed)
    await rt.refresh()
    monkeypatch.setattr("asyncio.create_subprocess_exec", docker)
    rt.docker = True

    await rt.restart("silabs-multiprotocol")
    assert calls[-1][-2:] == ("restart", "silabs-multiprotocol")
    assert changes == [1]  # integrations aren't told: they reconnect on their own
    with pytest.raises(LookupError):
        await rt.restart("otbr")  # not running
