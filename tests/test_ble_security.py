"""Secure channels: credentials are written to a BLE device only over a link secured first (LE Secure Connections),
through a radio that can pair, or over an unsecured one only when asked for by name."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from majordom_integration_sdk.discovery import (
    BLEConnection,
    BLEPairingError,
    BLEService,
    LESecureConnections,
    PairableBLEConnection,
    Unsecured,
)
from majordom_integration_sdk.testing import FakeBLESource


def pairable(calls: list[str] | None = None) -> Mock:
    connection = Mock(spec=PairableBLEConnection)
    connection.pair = AsyncMock(side_effect=lambda: calls.append("pair") if calls is not None else None)
    connection.unpair = AsyncMock(side_effect=lambda: calls.append("unpair") if calls is not None else None)
    connection.write = AsyncMock(side_effect=lambda *_, **__: calls.append("write") if calls is not None else None)
    return connection


async def test_le_secure_connections_pairs_before_any_write_and_unpairs_after():
    calls: list[str] = []
    connection = pairable(calls)

    async with LESecureConnections().secure(connection) as secured:
        await secured.write(1, b"secret")

    assert calls == ["pair", "write", "unpair"]


async def test_le_secure_connections_refuses_a_connection_that_cannot_pair():
    connection = Mock(spec=BLEConnection)

    with pytest.raises(BLEPairingError, match="can't pair"):
        async with LESecureConnections().secure(connection):
            pytest.fail("credentials would cross an unencrypted link")
    connection.write.assert_not_called()


async def test_a_failed_pairing_yields_nothing():
    connection = pairable()
    connection.pair.side_effect = BLEPairingError("authentication failed")

    with pytest.raises(BLEPairingError):
        async with LESecureConnections().secure(connection):
            pytest.fail("credentials would cross an unencrypted link")
    connection.write.assert_not_called()


async def test_the_bond_is_kept_when_asked():
    connection = pairable()

    async with LESecureConnections(forget=False).secure(connection):
        pass

    connection.unpair.assert_not_called()


async def test_unsecured_writes_as_is_and_says_so(caplog):
    connection = Mock(spec=BLEConnection)

    with caplog.at_level(logging.WARNING):
        async with Unsecured().secure(connection) as secured:
            assert secured is connection
    assert "unencrypted" in caplog.text
    assert not Unsecured.needs_pairing and LESecureConnections.needs_pairing


@pytest.fixture
async def radios():
    proxy = FakeBLESource("proxy", connection=Mock(spec=BLEConnection))  # stronger, can't pair
    adapter = FakeBLESource("adapter", connection=pairable(), can_pair=True)
    ble = BLEService(sources=[proxy, adapter])
    await ble.start()
    proxy.advertise("AA", rssi=-40)
    adapter.advertise("AA", rssi=-70)
    yield ble, proxy, adapter
    await ble.stop()


async def test_a_pairing_lease_takes_a_radio_that_can_pair_over_a_stronger_one(radios):
    ble, proxy, adapter = radios

    async with ble.exclusive("AA", owner="test", pairing=True) as lease:
        assert lease.source == "adapter"
    async with ble.exclusive("AA", owner="test") as lease:
        assert lease.source == "proxy"


async def test_a_pairing_lease_fails_when_no_radio_hearing_the_device_can_pair(radios):
    ble, proxy, adapter = radios
    adapter.can_pair = False

    with pytest.raises(BLEPairingError, match="can pair"):
        async with ble.exclusive("AA", owner="test", pairing=True, timeout=0.1):
            pytest.fail("leased a radio that can't pair")


@pytest.fixture
def adapter_client(monkeypatch):
    from majordom_integration_sdk.discovery import ble_discovery as module

    client = Mock(connect=AsyncMock(), pair=AsyncMock(), unpair=AsyncMock(), address="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(module, "BleakClient", lambda *args, **kwargs: client)
    return client


async def test_on_linux_a_local_adapter_pairs_through_its_own_bluez_agent(adapter_client, monkeypatch):
    from majordom_integration_sdk.discovery import LocalAdapter, bluez_pairing
    from majordom_integration_sdk.discovery import ble_discovery as module

    monkeypatch.setattr(module.sys, "platform", "linux")
    paired = AsyncMock()
    monkeypatch.setattr(bluez_pairing, "pair", paired)
    adapter = LocalAdapter("hci1")
    assert adapter.can_pair

    connection = await adapter.connect("AA:BB:CC:DD:EE:FF", lambda: None)
    assert isinstance(connection, PairableBLEConnection)
    await connection.pair()
    paired.assert_awaited_once_with("hci1", "AA:BB:CC:DD:EE:FF")
    adapter_client.pair.assert_not_called()  # bleak's brings no agent: Secure Connections fails without one
    await connection.unpair()
    adapter_client.unpair.assert_awaited_once()

    paired.side_effect = ConnectionError("org.bluez.Error.AuthenticationFailed: Authentication Failed")
    with pytest.raises(BLEPairingError, match="AuthenticationFailed"):
        await connection.pair()


async def test_elsewhere_the_os_pairs_on_its_own(adapter_client, monkeypatch):
    from bleak.exc import BleakError

    from majordom_integration_sdk.discovery import LocalAdapter
    from majordom_integration_sdk.discovery import ble_discovery as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    connection = await LocalAdapter().connect("AA:BB:CC:DD:EE:FF", lambda: None)

    await connection.pair()
    adapter_client.pair.assert_awaited_once()
    adapter_client.pair.side_effect = BleakError("refused")
    with pytest.raises(BLEPairingError, match="refused"):
        await connection.pair()


DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


@pytest.mark.parametrize(
    ("member", "body", "answer"),
    [
        ("RequestAuthorization", [DEVICE], ("", [])),  # Just Works on LE Secure Connections
        ("RequestConfirmation", [DEVICE, 1234], ("", [])),
        ("RequestPasskey", [DEVICE], "org.bluez.Error.Rejected"),
        ("RequestAuthorization", ["/org/bluez/hci0/dev_11_22_33_44_55_66"], "org.bluez.Error.Rejected"),
        ("RequestPinCode", [DEVICE], "org.bluez.Error.Rejected"),
        ("AuthorizeService", [DEVICE, "0000110b-0000-1000-8000-00805f9b34fb"], "org.bluez.Error.Rejected"),
        ("Release", [], ("", [])),
    ],
)
def test_the_agent_answers_only_for_the_device_being_paired(member, body, answer):
    from majordom_integration_sdk.discovery import bluez_pairing

    assert bluez_pairing.device_path("hci0", "aa:bb:cc:dd:ee:ff") == DEVICE
    assert bluez_pairing.agent_answer(member, body, DEVICE) == answer
