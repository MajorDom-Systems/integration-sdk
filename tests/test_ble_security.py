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


async def test_a_local_adapter_pairs_through_bleak_and_reports_failures(monkeypatch):
    from bleak.exc import BleakError

    from majordom_integration_sdk.discovery import LocalAdapter
    from majordom_integration_sdk.discovery import ble_discovery as module

    client = Mock(connect=AsyncMock(), pair=AsyncMock(), unpair=AsyncMock())
    monkeypatch.setattr(module, "BleakClient", lambda *args, **kwargs: client)
    adapter = LocalAdapter("hci0")
    assert adapter.can_pair

    connection = await adapter.connect("AA", lambda: None)
    assert isinstance(connection, PairableBLEConnection)
    await connection.pair()
    client.pair.assert_awaited_once()
    await connection.unpair()
    client.unpair.assert_awaited_once()

    client.pair.side_effect = BleakError("org.bluez.Error.AuthenticationFailed")
    with pytest.raises(BLEPairingError, match="AuthenticationFailed"):
        await connection.pair()
