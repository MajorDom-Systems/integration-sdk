"""Secure channels: how credentials (a Wi-Fi password, a Thread network key) are protected on a BLE connection before
they are written to a device. An integration picks one per device and opens it on the leased connection::

    async with ble.exclusive(address, owner=OWNER, pairing=channel.needs_pairing) as lease:
        async with channel.secure(await lease.connect()) as connection:
            await connection.write(rpc, credentials)

`LESecureConnections` encrypts the link by pairing. "Just Works" pairing, all a headless Hub can do without a
passkey, stops passive sniffing but not an active man in the middle during the pairing itself. A channel that
authenticates at the application layer (e.g. Improv's proposed SPAKE2+ setup code) would yield a connection that
encrypts the frames it writes instead, and works through any proxy.
"""

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Protocol

from .ble_discovery import BLEConnection, BLEPairingError, PairableBLEConnection

logger = logging.getLogger(__name__)


class SecureChannel(Protocol):
    needs_pairing: bool  # lease a source that can pair: `BLEService.exclusive(..., pairing=...)`

    def secure(self, connection: BLEConnection) -> contextlib.AbstractAsyncContextManager[BLEConnection]:
        """The connection to write credentials through, secured; raises `BLEPairingError` (fail closed)."""
        ...


class LESecureConnections:
    """Pairs before anything is written: the link is encrypted, or nothing is yielded. The device decides LE Secure
    Connections over legacy pairing (firmware built for it accepts nothing else). ``forget``: unpair once done, as the
    bond served only the setup."""

    needs_pairing = True

    def __init__(self, forget: bool = True):
        self._forget = forget

    @contextlib.asynccontextmanager
    async def secure(self, connection: BLEConnection) -> AsyncGenerator[BLEConnection]:
        if not isinstance(connection, PairableBLEConnection):
            raise BLEPairingError("This Bluetooth source can't pair, so it can't encrypt the link")
        await connection.pair()
        try:
            yield connection
        finally:
            if self._forget:
                with contextlib.suppress(Exception):
                    await connection.unpair()


class Unsecured:
    """Writes over the link as it is: only for firmware that can't pair, chosen explicitly."""

    needs_pairing = False

    @contextlib.asynccontextmanager
    async def secure(self, connection: BLEConnection) -> AsyncGenerator[BLEConnection]:
        logger.warning("Writing credentials over an unencrypted Bluetooth link")
        yield connection
