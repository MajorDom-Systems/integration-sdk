"""Hardware as peripherals, over fakes of /dev and /sys: no real stick is opened."""

import asyncio
from pathlib import Path

import pytest
from support import run, settle, state, until

from majordom_integration_sdk import Endpoint, Need, PeripheralChange, UsbDriver
from majordom_integration_sdk.runtime import (
    BluetoothAdapters,
    FixedPeripherals,
    FixedRadio,
    HostNetwork,
    PeripheralState,
    Pinned,
    Radio,
    Silabs,
    UsbScanner,
)
from majordom_integration_sdk.runtime.peripherals.hardware import host, usb
from majordom_integration_sdk.runtime.peripherals.providers.ble import cpc_hci
from majordom_integration_sdk.testing import FakePeripheral

NCP = {"baud": 115200, "flow_control": True}  # the firmware's serial settings
SKYCONNECT = "usb-Nabu_Casa_SkyConnect_v1.0_1-if00"


class Driver(UsbDriver):
    """Recognises the sticks named in ``recognised``, recording each one it opened."""

    firmwares = {
        "zigbee-ncp": [Endpoint("zigbee", "ezsp", "{device}", params=NCP)],
        "multipan-rcp": [Endpoint("multipan", "silabs-cpc", "{device}")],
    }

    def __init__(self, name: str = "silabs", **recognised: str):
        self.name, self.recognised, self.opened = name, recognised, []

    async def identify(self, path: str) -> str | None:
        self.opened.append(Path(path).name)
        return self.recognised.get(Path(path).name)


@pytest.fixture
def sticks(monkeypatch) -> dict[str, str]:
    """The sticks plugged in, by their by-id name; each is a known radio's USB id unless named "arduino"."""
    plugged: dict[str, str] = {}
    monkeypatch.setattr(usb, "_sticks", lambda: dict(plugged))
    monkeypatch.setattr(usb, "_nodes", lambda paths: dict(paths))
    monkeypatch.setattr(usb.os.path, "realpath", lambda path: path)
    monkeypatch.setattr(usb, "_usb_id", lambda path: ("1a86", "7523") if "arduino" in path else ("10c4", "ea60"))
    monkeypatch.setattr(usb, "POLL", 0.01)
    return plugged


@pytest.fixture(autouse=True)
def held(monkeypatch) -> dict[str, str]:
    """Other programs holding a stick open, by its file name; none unless a test says so."""
    holders: dict[str, str] = {}
    monkeypatch.setattr(usb, "_holder", lambda path, is_opening_allowed=False: holders.get(Path(path).name))
    return holders


def plug(sticks: dict[str, str], *names: str):
    sticks.update({name: f"/dev/serial/by-id/{name}" for name in names})


def zigbee() -> FakePeripheral:
    return FakePeripheral("zigbee", [Need("zigbee")])


async def test_each_stick_is_a_radio_serving_what_its_firmware_does(tmp_path, sticks):
    plug(sticks, "sky")
    scanner, integration = UsbScanner([Driver(sky="zigbee-ncp")]), zigbee()
    runtime = await run(tmp_path, scanner, integration)

    [radio] = scanner.radios
    assert runtime.peripherals == [scanner, radio, integration]
    assert (radio.name, radio.driver and radio.driver.name, radio.firmware) == ("sky", "silabs", "zigbee-ncp")
    assert integration.peripherals.endpoint("zigbee") == Endpoint("zigbee", "ezsp", "/dev/serial/by-id/sky", params=NCP)
    assert runtime.users(radio) == ["zigbee"]


async def test_only_known_radios_are_opened_until_asked(tmp_path, sticks):
    plug(sticks, "sky", "arduino")  # an Arduino resets whenever its port opens
    driver = Driver(sky="multipan-rcp", arduino="zigbee-ncp")
    scanner = UsbScanner([driver])
    runtime = await run(tmp_path, scanner)
    assert driver.opened == ["sky"]
    sky, arduino = sorted(scanner.radios, key=lambda r: r.name, reverse=True)
    assert (sky.firmware, arduino.firmware, arduino.provides) == ("multipan-rcp", None, [])

    await arduino.probe()  # the user asked: open it after all
    await settle(runtime)
    assert driver.opened == ["sky", "arduino"]
    assert arduino.firmware == "zigbee-ncp"


async def test_a_stick_another_program_holds_is_left_alone_until_probed(tmp_path, sticks, held):
    plug(sticks, "sky")
    held["sky"] = "zigbee2mqtt"
    driver, integration = Driver(sky="zigbee-ncp"), zigbee()
    scanner = UsbScanner([driver])
    runtime = await run(tmp_path, scanner, integration)
    assert driver.opened == []  # never opened: probing would reset the radio under zigbee2mqtt
    assert scanner.radios[0].held_by == "zigbee2mqtt"
    assert state(runtime, integration) == PeripheralState.WAITING

    await scanner.radios[0].probe()  # the user claims it anyway
    await settle(runtime)
    assert scanner.radios[0].held_by is None
    assert state(runtime, integration) == PeripheralState.RUNNING


async def test_a_stick_is_used_once_the_other_program_lets_go(tmp_path, sticks, held):
    plug(sticks, "sky")
    held["sky"] = "node"
    scanner, integration = UsbScanner([Driver(sky="zigbee-ncp")]), zigbee()
    runtime = await run(tmp_path, scanner, integration)

    held.clear()
    await until(runtime, lambda: state(runtime, integration) == PeripheralState.RUNNING)


async def test_a_stick_held_out_of_sight_isnt_looked_at_again_and_again(tmp_path, sticks, monkeypatch):
    plug(sticks, "sky")  # its holder locked it: no open file of it can be read
    monkeypatch.setattr(
        usb, "_holder", lambda path, is_opening_allowed=False: is_opening_allowed and "another program" or None
    )
    scanner, rescans = UsbScanner([Driver(sky="zigbee-ncp")]), []

    async def rescan():
        rescans.append(scanner.radios[0].held_by)

    monkeypatch.setattr(scanner, "_rescan", rescan)
    await run(tmp_path, scanner)
    assert scanner.radios[0].held_by == "another program"
    await asyncio.sleep(0.1)  # ten looks
    assert rescans == []


async def test_a_stick_plugged_in_or_out_is_followed(tmp_path, sticks):
    scanner, integration = UsbScanner([Driver(sky="zigbee-ncp")]), zigbee()
    runtime = await run(tmp_path, scanner, integration)

    plug(sticks, "sky")
    await until(runtime, lambda: state(runtime, integration) == PeripheralState.RUNNING)
    sticks.clear()
    await until(runtime, lambda: state(runtime, integration) == PeripheralState.WAITING)
    assert scanner.radios == [] and integration.calls == ["start", "stop"]


async def test_a_stick_that_comes_back_on_another_tty_is_followed(tmp_path, sticks, monkeypatch):
    """A USB bus reset re-enumerates sticks onto new ttys under the same by-id name, faster than the watcher's
    debounce: what uses it must not keep the old device node (it may be another device now)."""
    nodes = {"sky": "/dev/ttyUSB0"}
    monkeypatch.setattr(usb.os.path, "realpath", lambda path: nodes.get(Path(path).name, path))
    monkeypatch.setattr(usb, "_nodes", lambda paths: {name: nodes[name] for name in paths})
    plug(sticks, "sky")
    integration = FakePeripheral("zigbee", [Need("zigbee")])
    runtime = await run(tmp_path, UsbScanner([Driver(sky="zigbee-ncp")]), integration)
    assert integration.peripherals.endpoint("zigbee").url == "/dev/ttyUSB0"

    nodes["sky"] = "/dev/ttyUSB4"
    await until(runtime, lambda: integration.peripherals.endpoint("zigbee").url == "/dev/ttyUSB4")


async def test_a_driver_an_integration_brings_along_recognises_what_the_others_dont(tmp_path, sticks):
    class Zigpy(Driver):
        firmwares = {"deconz": [Endpoint("zigbee", "deconz", "{device}")]}

    plug(sticks, "conbee")
    zigpy = Zigpy("zigpy", conbee="deconz")
    integration = FakePeripheral("zigbee", [Need("zigbee")])
    scanner = UsbScanner([Driver()])  # the SDK's drivers know nothing of it
    runtime = await run(tmp_path, scanner, zigpy, integration)

    await until(runtime, lambda: state(runtime, integration) == PeripheralState.RUNNING)
    assert integration.peripherals.endpoint("zigbee") == Endpoint("zigbee", "deconz", "/dev/serial/by-id/conbee")
    assert zigpy.opened == ["conbee"]
    assert runtime.providers(scanner) == {"usb-driver": "zigpy"}


async def test_one_rescan_at_a_time_opens_a_stick_once(tmp_path, sticks):
    opening: list[str] = []
    at_once: list[int] = []

    class Slow(Driver):
        async def identify(self, path: str) -> str | None:
            opening.append(path)
            at_once.append(len(opening))
            await asyncio.sleep(0.02)
            opening.remove(path)
            return await super().identify(path)

    plug(sticks, "conbee")
    scanner, slow = UsbScanner([Driver()]), Slow("zigpy", conbee="zigbee-ncp")
    runtime = await run(tmp_path, scanner)
    scanner.drivers.append(slow)  # as if an integration's driver got bound
    change = PeripheralChange("usb-driver", None, None)
    await asyncio.gather(scanner.needed_peripheral_did_change(change), scanner.needed_peripheral_did_change(change))
    await until(runtime, lambda: scanner.radios[0].firmware == "zigbee-ncp")
    await asyncio.sleep(0.05)
    assert at_once == [1]


async def test_a_probe_that_never_answers_gives_up(tmp_path, sticks, monkeypatch):
    class Silent(Driver):
        async def identify(self, path: str) -> str | None:
            await asyncio.Event().wait()  # e.g. bellows resetting a port someone else holds
            return None

    monkeypatch.setattr(usb, "PROBE_TIMEOUT", 0.01)
    plug(sticks, "odd")
    scanner = UsbScanner([Silent()])
    async with asyncio.timeout(1):
        await run(tmp_path, scanner)
    assert scanner.radios[0].driver is None


async def test_a_radio_is_released_before_it_is_flashed_and_used_again_after(tmp_path, sticks):
    seen_while_flashed = []

    class Flashing(Driver):
        def flashable(self, path: str, channel: str) -> list[str]:
            return ["zigbee-ncp", "multipan-rcp"]

        async def flash(self, path: str, firmware: str, channel: str, cache: Path):
            seen_while_flashed.append(state(runtime, integration))
            self.recognised["sky"] = firmware

    plug(sticks, "sky")
    integration = FakePeripheral("zigbee", [Need("zigbee")], answer=True)
    multipan = FakePeripheral("multipan", [Need("multipan")])
    scanner = UsbScanner([Flashing(sky="zigbee-ncp")])
    runtime = await run(tmp_path, scanner, integration, multipan)

    await scanner.radios[0].flash("multipan-rcp")
    await settle(runtime)
    assert seen_while_flashed == [PeripheralState.WAITING]  # it let go of the stick first
    assert (state(runtime, integration), state(runtime, multipan)) == (PeripheralState.WAITING, PeripheralState.RUNNING)


async def test_a_radio_whose_flash_failed_serves_what_it_runs_now(tmp_path, sticks):
    class Bricking(Driver):
        def flashable(self, path: str, channel: str) -> list[str]:
            return ["multipan-rcp"]

        async def flash(self, path: str, firmware: str, channel: str, cache: Path):
            del self.recognised["sky"]  # left in its bootloader
            raise RuntimeError("the flasher crashed")

    plug(sticks, "sky")
    integration, scanner = zigbee(), UsbScanner([Bricking(sky="zigbee-ncp")])
    runtime = await run(tmp_path, scanner, integration)
    with pytest.raises(RuntimeError, match="crashed"):
        await scanner.radios[0].flash("multipan-rcp")
    await settle(runtime)
    assert (scanner.radios[0].firmware, scanner.radios[0].provides) == (None, [])
    assert state(runtime, integration) == PeripheralState.WAITING


async def test_the_firmware_offered_follows_the_radios_channel(tmp_path):
    radio = Radio(SKYCONNECT, f"/dev/serial/by-id/{SKYCONNECT}", lambda: [], Silabs(), "thread-rcp")
    runtime = await run(tmp_path, FakePeripheral("board", provides=[radio]), zigbee())
    assert radio.options == []  # stable channel: nothing verified to flash yet

    await runtime.update_settings(radio, {"firmware_channel": "dev"})
    assert radio.options == ["zigbee-ncp", "thread-rcp", "multipan-rcp"]
    assert runtime.reach(Silabs().endpoints("multipan-rcp", "x")) == []  # nothing here needs what it would serve
    assert runtime.reach(Silabs().endpoints("zigbee-ncp", "x")) == ["zigbee"]


async def test_a_multiprotocol_firmware_that_serves_bluetooth_offers_it_too(tmp_path, sticks, monkeypatch):
    asked: list[str] = []

    async def has_bluetooth(path: str) -> bool:
        asked.append(path)
        return True

    monkeypatch.setattr(cpc_hci, "has_bluetooth", has_bluetooth)
    plug(sticks, "sky", "ncp")
    scanner = UsbScanner([Driver(sky="multipan-rcp", ncp="zigbee-ncp")])
    await run(tmp_path, scanner)

    sky, ncp = sorted(scanner.radios, key=lambda r: r.name, reverse=True)
    assert asked == ["/dev/serial/by-id/sky"]  # a multi-PAN firmware only, asked once
    assert sky.provides[-1] == Endpoint("ble", "silabs-cpc-hci", "/dev/serial/by-id/sky", params={"hci": None})
    assert [e.family for e in ncp.provides] == ["zigbee"]


async def test_the_cpc_probe_says_no_until_verified_on_hardware():
    assert await cpc_hci.has_bluetooth("/dev/ttyUSB0") is False
    with pytest.raises(ConnectionError, match="no Bluetooth adapter"):
        cpc_hci.source_of(Endpoint("ble", "silabs-cpc-hci", "/dev/ttyUSB0", params={"hci": None}))


async def test_soldered_radios_are_used_without_asking_wired_as_the_board_says(tmp_path):
    (tmp_path / "ttyAMA3").touch()
    driver = Driver(ttyAMA3="zigbee-ncp")
    board = FixedPeripherals(
        FixedRadio("mg26", str(tmp_path / "ttyAMA3"), "silabs", "zigbee-ncp", {"flow_control": False}),
        FixedRadio("absent", str(tmp_path / "ttyAMA4"), "silabs", "zigbee-ncp"),
        FixedRadio("unknown", str(tmp_path / "ttyAMA3"), "silabs"),
        drivers=[driver],
    )
    integration = zigbee()
    runtime = await run(tmp_path, board, integration)

    assert [radio.name for radio in board.provides] == ["mg26", "unknown"]  # one that isn't there is left out
    expected = Endpoint("zigbee", "ezsp", str(tmp_path / "ttyAMA3"), params={"baud": 115200, "flow_control": False})
    assert integration.peripherals.endpoint("zigbee") == expected
    assert driver.opened == ["ttyAMA3"]  # only the one whose firmware the board doesn't tell
    assert runtime.status(board.provides[1]).state == PeripheralState.RUNNING


async def test_the_host_offers_its_network_and_every_adapter_with_its_index(tmp_path, monkeypatch):
    found = ["/sys/class/bluetooth/hci1", "/sys/class/bluetooth/hci0", "/sys/class/bluetooth/hci0:1"]
    monkeypatch.setattr(host, "glob", lambda pattern: list(found))
    monkeypatch.setattr(host.sys, "platform", "linux")
    monkeypatch.setattr(host, "POLL", 0.01)
    adapters = BluetoothAdapters()
    sniffer = FakePeripheral("sniffer", [Need("ip"), Need("ble", is_many=True)], answer=True)
    runtime = await run(tmp_path, HostNetwork(), adapters, sniffer)
    assert runtime.bound(sniffer) == {
        "ip": Endpoint("ip", is_shared=True),
        "ble": [
            Endpoint("ble", "bluez", "hci0", params={"index": "0"}),
            Endpoint("ble", "bluez", "hci1", params={"index": "1"}),
        ],
    }

    found.append("/sys/class/bluetooth/hci2")  # plugged in
    await until(runtime, lambda: len(sniffer.peripherals.endpoints("ble")) == 3)


def test_off_linux_the_host_has_its_default_adapter(monkeypatch):
    monkeypatch.setattr(host.sys, "platform", "darwin")
    assert host._adapters() == [""]


async def test_endpoints_pinned_first_win_and_come_and_go(tmp_path, sticks):
    plug(sticks, "sky")
    mine = Endpoint("zigbee", "ezsp", "socket://192.168.1.5:6638", is_shared=True)
    pinned, integration = Pinned(mine), FakePeripheral("zigbee", [Need("zigbee")], answer=True)
    runtime = await run(tmp_path, pinned, UsbScanner([Driver(sky="zigbee-ncp")]), integration)
    assert integration.peripherals.endpoint("zigbee") == mine

    await pinned.unpin("zigbee")
    await settle(runtime)
    assert integration.peripherals.endpoint("zigbee").url == "/dev/serial/by-id/sky"
    await pinned.pin(mine)
    await settle(runtime)
    assert runtime.providers(integration) == {"zigbee": "pinned"}
