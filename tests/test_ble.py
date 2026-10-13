"""BLEService: what listeners hear from several radios, and which radio connects."""

import asyncio
from uuid import UUID

import pytest

from majordom_integration_sdk.discovery import BLEDiscoveryInfo, BLEMatcher, BLEService
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
    near, far = FakeBLESource("near", connection=object()), FakeBLESource("far", connection=object())
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


async def test_pauses_nest_and_a_source_added_meanwhile_waits(radios):
    ble, near, _ = radios
    async with ble.paused():
        async with ble.paused():
            assert near.on_advertisement is None
        assert near.on_advertisement is None
        proxy = FakeBLESource("proxy")
        await ble.add_source(proxy)
        assert proxy.on_advertisement is None
    assert near.on_advertisement and proxy.on_advertisement


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
    from majordom_integration_sdk.discovery import LocalAdapter
    from majordom_integration_sdk.discovery import ble_discovery as module

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


async def test_a_device_is_seen_as_the_strongest_radio_hears_it(radios):
    ble, near, far = radios
    far.advertise("AA", rssi=-90, service_uuids=[str(MATTER)])
    near.advertise("AA", rssi=-50, service_uuids=[str(MATTER)])
    await settle()

    info = ble.devices()[0]
    assert (info.source, info.rssi) == ("near", -50)
    assert info.last_seen > 0
    assert ble.best_device("AA").details == "near"  # bleak's device, for connecting through that radio
    assert ble.best_device("BB") is None


async def test_a_service_data_matcher_needs_the_data(radios):
    ble, near, _ = radios
    listener = Listener()
    ble.register(listener, [BLEMatcher(service_data_uuid=MATTER)])

    near.advertise("AA", service_uuids=[str(MATTER)])  # only listed: no data to read
    near.advertise("BB", service_data={str(MATTER): b"\x00"})
    await settle()
    assert [i.device.address for i in listener.found] == ["BB"]
