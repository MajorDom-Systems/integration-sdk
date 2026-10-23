"""The Bluetooth router: what listeners hear from several radios, and which radio connects."""

import asyncio
from unittest.mock import Mock
from uuid import UUID

import pytest

from majordom_integration_sdk.capabilities import BLEBusyError, BLEConnection, BLEDiscoveryInfo, BLEMatcher
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.testing import FakeBLESource

MATTER = UUID("0000fff6-0000-1000-8000-00805f9b34fb")
APPLE = 0x004C


class Listener:
    def __init__(self):
        self.found: list[BLEDiscoveryInfo] = []
        self.updated: list[BLEDiscoveryInfo] = []
        self.removed: list[BLEDiscoveryInfo] = []

    async def ble_did_discover_device(self, ble, info):
        self.found.append(info)

    async def ble_did_update_device(self, ble, info):
        self.updated.append(info)

    async def ble_did_remove_device(self, ble, info):
        self.removed.append(info)


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
async def radios():
    near, far = (
        FakeBLESource("near", connection=Mock(spec=BLEConnection)),
        FakeBLESource("far", connection=Mock(spec=BLEConnection)),
    )
    ble = BLEService(sources=[near, far])
    await ble.start()
    yield ble, near, far
    await ble.stop()


async def test_listeners_of_the_same_service_all_hear_it(radios):
    ble, near, _ = radios
    first, second = Listener(), Listener()
    ble.register(first, {MATTER})
    ble.register(second, {MATTER})

    near.advertise("AA", service_data={str(MATTER): b"\x00"})
    await settle()
    assert len(first.found) == len(second.found) == 1


async def test_matchers_select_by_manufacturer_data_and_name(radios):
    ble, near, _ = radios
    homekit, named = Listener(), Listener()
    ble.register(homekit, [BLEMatcher(manufacturer_id=APPLE, manufacturer_data_start=b"\x06")])
    ble.register(named, [BLEMatcher(local_name_prefix="Improv")])

    near.advertise("AA", manufacturer_data={APPLE: b"\x10\x05"})  # an iPhone nearby
    near.advertise("BB", manufacturer_data={APPLE: b"\x06\x31"}, local_name="Improv-1")
    await settle()
    assert [i.device.address for i in homekit.found] == ["BB"]
    assert [i.device.address for i in named.found] == ["BB"]


async def test_a_device_heard_by_two_radios_is_one_device_seen_by_the_stronger(radios):
    ble, near, far = radios
    listener = Listener()
    ble.register(listener, {MATTER})

    far.advertise("AA", rssi=-90, service_uuids=[str(MATTER)])
    near.advertise("AA", rssi=-50, service_uuids=[str(MATTER)], local_name="Bulb")
    far.advertise("AA", rssi=-90, service_uuids=[str(MATTER)])  # no scan response: no name
    await settle()
    assert len(listener.found) == 1
    assert [i.source for i in listener.updated] == ["near"]
    assert ble.devices()[0].advertisement.local_name == "Bulb"


async def test_a_device_is_removed_once_no_radio_hears_it(radios):
    ble, near, far = radios
    listener = Listener()
    ble.register(listener, {MATTER})
    near.advertise("AA", service_uuids=[str(MATTER)])
    await settle()

    ble._removal_grace_sec = 0
    await ble.perform_scan()
    assert [i.device.address for i in listener.removed] == ["AA"]
    assert ble.devices() == []


async def test_cancelled_listeners_hear_nothing(radios):
    ble, near, _ = radios
    listener = Listener()
    cancel = ble.register(listener, {MATTER})
    cancel()
    cancel()

    near.advertise("AA", service_uuids=[str(MATTER)])
    await settle()
    assert listener.found == []


async def test_connects_through_the_stronger_radio_and_falls_back(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90)
    near.advertise("AA", rssi=-50)
    await settle()

    assert await ble.connect("AA") is near.connection
    near.connection = None
    assert await ble.connect("AA") is far.connection
    far.connection = None
    with pytest.raises(ConnectionError, match="near.*far"):
        await ble.connect("AA")


async def test_a_lease_takes_the_radio_hearing_the_device_best(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90)
    near.advertise("AA", rssi=-50)
    await settle()

    async with ble.exclusive("AA", owner="matter") as lease:
        assert lease.source == "near"
        assert await lease.connect() is near.connection
        assert (near.connected, far.connected) == (["AA"], [])


async def test_a_lease_stops_only_its_radio_scanning_until_released(radios):
    ble, near, far = radios
    near.advertise("AA")
    await settle()

    async with ble.exclusive("AA", owner="matter"):
        assert near.on_advertisement is None
        assert far.on_advertisement is not None
        await ble.scan_actively(0)
        await ble._revive_silent()
        await ble.add_source(proxy := FakeBLESource("proxy"))
        assert near.on_advertisement is None
        assert proxy.on_advertisement is not None
    assert near.on_advertisement is not None


class PausableFakeBLESource(FakeBLESource):
    def __init__(self, name: str):
        super().__init__(name)
        self.calls: list[str] = []

    async def start(self, on_advertisement):
        self.calls.append("start")
        await super().start(on_advertisement)

    async def stop(self):
        self.calls.append("stop")
        await super().stop()

    async def pause_scanning(self):
        self.calls.append("pause")

    async def resume_scanning(self):
        self.calls.append("resume")


async def test_a_lease_pauses_a_radio_that_can_pause_instead_of_stopping_it(radios):
    ble, _, _ = radios
    await ble.add_source(proxy := PausableFakeBLESource("proxy"))
    proxy.advertise("AA", rssi=-10)
    await settle()

    async with ble.exclusive("AA", owner="matter") as lease:
        assert lease.source == "proxy"
        assert proxy.calls == ["start", "pause"]
    assert proxy.calls == ["start", "pause", "resume"]


async def test_a_second_lease_waits_then_takes_the_next_radio(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90)
    near.advertise("AA", rssi=-50)
    await settle()

    async with ble.exclusive("AA", owner="matter"):
        started = asyncio.get_running_loop().time()
        async with ble.exclusive("AA", owner="improv", wait=0.05) as lease:
            assert lease.source == "far"
            assert asyncio.get_running_loop().time() - started >= 0.05


async def test_a_connect_skips_leased_radios_and_fails_when_all_are(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90)
    near.advertise("AA", rssi=-50)
    await settle()

    async with ble.exclusive("AA", owner="matter"):
        assert await ble.connect("AA") is far.connection
        async with ble.exclusive("AA", owner="improv", wait=0):
            with pytest.raises(BLEBusyError):
                await ble.connect("AA")


async def test_a_lease_cycles_through_busy_radios_until_it_gives_up(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90)
    near.advertise("AA", rssi=-50)
    await settle()

    async with ble.exclusive("AA", owner="matter"), ble.exclusive("AA", owner="matter", wait=0):
        started = asyncio.get_running_loop().time()
        with pytest.raises(BLEBusyError, match="hear AA are busy"):
            async with ble.exclusive("AA", owner="improv", timeout=0.1, wait=0.02):
                pass
        assert asyncio.get_running_loop().time() - started >= 0.1


async def test_a_lease_freed_meanwhile_is_taken_on_the_next_round(radios):
    ble, near, far = radios
    near.advertise("AA", rssi=-50)
    await settle()

    held = asyncio.Event()

    async def hold():
        async with ble.exclusive("AA", owner="matter"):
            held.set()
            await asyncio.sleep(0.03)

    task = asyncio.create_task(hold())
    await held.wait()
    async with ble.exclusive("AA", owner="improv", wait=0.01) as lease:  # only near hears it
        assert lease.source == "near"
    await task


async def test_a_cancelled_lease_releases_its_radio(radios):
    ble, near, _ = radios
    near.advertise("AA")
    await settle()
    held = asyncio.Event()

    async def hold():
        async with ble.exclusive("AA", owner="matter"):
            held.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await held.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert near.on_advertisement is not None
    async with ble.exclusive("AA", owner="improv", wait=0) as lease:
        assert lease.source == "near"


async def test_a_lease_cancelled_while_waiting_holds_nothing(radios):
    ble, near, far = radios
    near.advertise("AA")
    await settle()

    async with ble.exclusive("AA", owner="matter"):
        waiting = asyncio.create_task(ble.exclusive("AA", owner="improv", wait=1).__aenter__())
        await settle()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    async with ble.exclusive("AA", owner="improv", wait=0) as lease:
        assert lease.source == "near"
    assert near.on_advertisement and far.on_advertisement


async def test_a_listener_registered_later_hears_devices_already_around(radios):
    ble, near, _ = radios
    near.advertise("AA", service_uuids=[str(MATTER)])
    await settle()

    listener = Listener()
    ble.register(listener, {MATTER})
    await settle()
    assert [i.device.address for i in listener.found] == ["AA"]


async def test_a_silent_radio_is_restarted(radios):
    ble, near, far = radios
    near.on_advertisement = None  # bluetoothd restarted: the scanner is gone without a word
    far.advertise("AA")
    await settle()

    ble._silence_sec = 0.01
    await asyncio.sleep(0.02)
    far.advertise("AA")
    await ble._revive_silent()
    assert near.on_advertisement is not None  # listening again


async def test_a_local_adapter_stops_scanning_while_it_connects(monkeypatch):
    from majordom_integration_sdk.runtime.peripherals.providers.ble import bluez as module
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

    scanning = []

    class Scanner:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            scanning.append(True)

        async def stop(self):
            scanning.append(False)

    class Client:
        def __init__(self, device, **kwargs):
            pass

        async def connect(self):
            assert scanning[-1] is False, "connecting while discovering: many adapters stall"

    monkeypatch.setattr(module, "BleakScanner", Scanner)
    monkeypatch.setattr(module, "BleakClient", Client)
    adapter = LocalAdapter("hci0")
    await adapter.start(lambda device, adv: None)

    await adapter.connect("AA", lambda: None)
    assert scanning == [True, False, True]


def test_a_local_connection_reports_the_mtu_bluez_negotiated():
    from types import SimpleNamespace

    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import _BleakConnection

    # bleak reports BlueZ's MTU as 23 until it is acquired, while every characteristic already knows the real one
    characteristics = {1: SimpleNamespace(max_write_without_response_size=244)}
    client = SimpleNamespace(mtu_size=23, services=SimpleNamespace(characteristics=characteristics))

    assert _BleakConnection(client, "hci0").mtu == 247


async def test_a_device_is_seen_as_the_strongest_radio_hears_it(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90, service_uuids=[str(MATTER)])
    near.advertise("AA", rssi=-50, service_uuids=[str(MATTER)])
    await settle()

    info = ble.devices()[0]
    assert (info.source, info.rssi) == ("near", -50)
    assert info.last_seen > 0
    assert info.device.details == "near"  # bleak's device as that radio has it


async def test_a_service_data_matcher_needs_the_data(radios):
    ble, near, _ = radios
    listener = Listener()
    ble.register(listener, [BLEMatcher(service_data_uuid=MATTER)])

    near.advertise("AA", service_uuids=[str(MATTER)])  # only listed: no data to read
    near.advertise("BB", service_data={str(MATTER): b"\x00"})
    await settle()
    assert [i.device.address for i in listener.found] == ["BB"]


@pytest.fixture
def scanners(monkeypatch):
    """The scanners local adapters open: their arguments, in order."""
    from majordom_integration_sdk.runtime.peripherals.providers.ble import bluez as module

    opened: list[dict] = []

    class Scanner:
        def __init__(self, detection_callback=None, scanning_mode="active", bluez=None, **kwargs):
            self.args = {
                "mode": scanning_mode,
                "patterns": (bluez or {}).get("or_patterns"),
                "on_detection": detection_callback,
                "scanning": False,
            }

        async def start(self):
            if self.args["mode"] == "passive" and opened and opened[-1].get("refuse_passive"):
                raise RuntimeError("passive scanning needs BlueZ's advertisement monitor")
            self.args["scanning"] = True
            opened.append(self.args)

        async def stop(self):
            self.args["scanning"] = False

    monkeypatch.setattr(module, "BleakScanner", Scanner)
    return opened


async def test_local_adapters_hear_every_advertiser_and_the_matchers_choose(scanners):
    from bleak.assigned_numbers import AdvertisementDataType as T
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

    ble = BLEService(sources=[LocalAdapter("hci0")])
    matter, everything = Listener(), Listener()
    ble.register(matter, {MATTER})
    ble.register(everything, [BLEMatcher()])
    await ble.start()
    assert scanners[-1]["mode"] == "passive"
    assert scanners[-1]["patterns"] == [(0, T.FLAGS, b"\x02"), (0, T.FLAGS, b"\x06"), (0, T.FLAGS, b"\x1a")]

    hear = scanners[-1]["on_detection"]
    hear(BLEDevice("AA", None, {}), AdvertisementData(None, {APPLE: b"\x10\x05"}, {}, [], None, -60, ()))
    hear(BLEDevice("BB", None, {}), AdvertisementData(None, {}, {str(MATTER): b"\x00"}, [], None, -60, ()))
    await settle()
    assert [i.device.address for i in matter.found] == ["BB"]
    assert len(everything.found) == 2
    await ble.stop()


async def test_local_adapters_scan_passively_and_actively_in_bursts(scanners):
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

    ble = BLEService(sources=[LocalAdapter("hci0")])
    await ble.start()
    assert scanners[-1]["mode"] == "passive"

    await ble.scan_actively(0.05)  # a pairing window: names come in scan responses
    assert scanners[-1]["mode"] == "active"
    await asyncio.sleep(0.1)
    assert scanners[-1]["mode"] == "passive"
    await ble.stop()


async def test_an_adapter_without_passive_scanning_scans_actively(scanners, caplog):
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

    ble = BLEService(sources=[LocalAdapter("hci0")])
    await ble.start()
    await ble.scan_actively(0.01)
    scanners[-1]["refuse_passive"] = True
    await asyncio.sleep(0.05)  # the burst ends: back to passive, refused
    assert scanners[-1]["mode"] == "active"
    assert "can't scan passively" in caplog.text
    await ble.stop()


async def test_a_rescan_waits_until_a_local_adapter_has_connected(scanners, monkeypatch):
    from majordom_integration_sdk.runtime.peripherals.providers.ble import bluez as module
    from majordom_integration_sdk.runtime.peripherals.providers.ble.bluez import LocalAdapter

    connecting, connected = asyncio.Event(), asyncio.Event()

    class Client:
        def __init__(self, device, **kwargs):
            pass

        async def connect(self):
            connecting.set()
            await connected.wait()

    monkeypatch.setattr(module, "BleakClient", Client)
    ble = BLEService(sources=[LocalAdapter("hci0")])
    await ble.start()
    connection = asyncio.create_task(ble.connect("AA"))
    await connecting.wait()
    burst = asyncio.create_task(ble.scan_actively(1))  # a pairing window opens meanwhile
    await settle()
    assert not any(s["scanning"] for s in scanners), "discovering while connecting: many adapters stall"

    connected.set()
    await connection
    await burst
    assert [s["mode"] for s in scanners if s["scanning"]] == ["active"]
    await ble.stop()


class ScanModeFakeBLESource(FakeBLESource):
    def __init__(self, name: str):
        super().__init__(name)
        self.modes: list[bool] = []

    async def set_scan_mode(self, active: bool):
        self.modes.append(active)


async def test_a_pairing_window_switches_every_source_that_can_to_active_scanning():
    radio = ScanModeFakeBLESource("hci0")
    ble = BLEService(sources=[radio, FakeBLESource("proxy")])  # one that can't switch is left as it is
    await ble.start()
    assert radio.modes == [False]  # passive from the start

    await ble.scan_actively(0.02)
    assert radio.modes == [False, True]
    await asyncio.sleep(0.05)
    assert radio.modes == [False, True, False]
    await ble.stop()


async def test_a_proxy_without_a_free_connection_slot_is_tried_last():
    full = FakeBLESource("full", connection=Mock(spec=BLEConnection), free_slots=0)
    free = FakeBLESource("free", connection=Mock(spec=BLEConnection), free_slots=2)
    ble = BLEService(sources=[full, free])
    await ble.start()
    full.advertise("AA", rssi=-40)
    free.advertise("AA", rssi=-80)
    await settle()

    await ble.connect("AA")
    assert (full.connected, free.connected) == ([], ["AA"])
    await ble.stop()


async def test_the_runtime_binds_and_unbinds_radios_as_sources():
    from majordom_integration_sdk import Endpoint, PeripheralChange

    ble = BLEService()
    await ble.start()
    proxy = FakeBLESource("esphome:aa")
    lent = Endpoint("ble", "esphome-proxy", "esphome:aa", instance=proxy)

    await ble.peripheral_did_change(PeripheralChange("ble", None, [lent]))
    assert proxy.on_advertisement is not None  # heard through
    await ble.peripheral_did_change(PeripheralChange("ble", [lent], []))
    assert proxy.on_advertisement is None
    await ble.stop()


async def test_a_manufacturer_id_alone_selects_devices_that_advertise_no_service(radios):
    """Ported from feature/ble-manufacturer-filter: HomeKit (HAP-BLE) accessories advertise Apple manufacturer data
    and no service UUID."""
    ble, near, _ = radios
    by_service, by_manufacturer = Listener(), Listener()
    ble.register(by_service, {MATTER})
    ble.register(by_manufacturer, [BLEMatcher(manufacturer_id=APPLE)])

    near.advertise("AA", service_uuids=[str(MATTER)])
    near.advertise("BB", manufacturer_data={APPLE: b"\x06\x31"})
    near.advertise("CC", manufacturer_data={0x0059: b"\x01"})
    await settle()
    assert [i.device.address for i in by_service.found] == ["AA"]
    assert [i.device.address for i in by_manufacturer.found] == ["BB"]


async def test_changed_manufacturer_data_is_an_update(radios):
    ble, near, _ = radios
    listener = Listener()
    ble.register(listener, [BLEMatcher(manufacturer_id=APPLE)])

    for data in (b"\x06\x31\x01", b"\x06\x31\x01", b"\x06\x31\x02"):
        near.advertise("BB", manufacturer_data={APPLE: data})
        await settle()
    assert (len(listener.found), len(listener.updated)) == (1, 1)


async def test_fake_offers_lend_the_router_what_an_integration_offers():
    from majordom_integration_sdk import Endpoint
    from majordom_integration_sdk.testing import FakeOffers

    ble = BLEService()
    await ble.start()
    offers, proxy = FakeOffers(ble), FakeBLESource("esphome:aa")
    lent = Endpoint("ble", "esphome-proxy", "esphome:aa", instance=proxy)

    await offers.offer(lent)
    assert offers.offered == [lent] and proxy.on_advertisement is not None
    await offers.withdraw(lent)
    assert offers.offered == [] and proxy.on_advertisement is None
    await ble.stop()
