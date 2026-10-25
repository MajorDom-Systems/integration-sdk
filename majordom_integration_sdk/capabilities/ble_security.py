"""Secure channels: how credentials (a Wi-Fi password, a Thread network key) are protected on a BLE connection before
they are written to a device. An integration picks one per device and opens it on the leased connection::

    async with ble.exclusive(address, owner=OWNER, is_pairing_needed=channel.is_pairing_needed) as lease:
        async with channel.secure(await lease.connect()) as connection:
            await connection.write(rpc, credentials)

`LESecureConnections` encrypts the link by pairing. "Just Works" pairing, all a headless Hub can do, stops passive
sniffing but not an active man in the middle during the pairing itself. A channel that
authenticates at the application layer (e.g. Improv's proposed SPAKE2+ setup code) would yield a connection that
encrypts the frames it writes instead, and works through any proxy.
"""

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Protocol

from .ble import BLEConnection, BLEPairingError, PairableBLEConnection

logger = logging.getLogger(__name__)


class SecureChannel(Protocol):
    is_pairing_needed: bool  # lease a source that can pair: `BLERouter.exclusive(..., pairing=...)`

    def secure(self, connection: BLEConnection) -> contextlib.AbstractAsyncContextManager[BLEConnection]:
        """The connection to write credentials through, secured; raises `BLEPairingError` (fail closed)."""
        ...


class LESecureConnections:
    """Pairs before anything is written: the link is encrypted, or nothing is yielded. The device decides LE Secure
    Connections over legacy pairing (firmware built for it accepts nothing else). It unpairs once done, as the
    pairing served only the setup, unless ``is_pairing_kept``."""

    is_pairing_needed = True

    def __init__(self, is_pairing_kept: bool = False):
        self._is_pairing_kept = is_pairing_kept

    @contextlib.asynccontextmanager
    async def secure(self, connection: BLEConnection) -> AsyncGenerator[BLEConnection]:
        if not isinstance(connection, PairableBLEConnection):
            raise BLEPairingError("This Bluetooth source can't pair, so it can't encrypt the link")
        await connection.pair()
        try:
            yield connection
        finally:
            if not self._is_pairing_kept:
                with contextlib.suppress(Exception):
                    await connection.unpair()


class Unsecured:
    """Writes over the link as it is: only for firmware that can't pair, chosen explicitly."""

    is_pairing_needed = False

    @contextlib.asynccontextmanager
    async def secure(self, connection: BLEConnection) -> AsyncGenerator[BLEConnection]:
        logger.warning("Writing credentials over an unencrypted Bluetooth link")
        yield connection
