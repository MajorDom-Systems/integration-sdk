"""The host's Bluetooth adapters as ble/bluez sources of the router, through bleak (BlueZ on Linux, the OS's own
stack elsewhere), and pairing through BlueZ with an agent of our own, scoped to the one device being paired.

LE Secure Connections "Just Works" still asks the host to confirm (the kernel's confirm hint), and bluetoothd turns that
into an agent call: without an agent the pairing fails ("Numeric comparison failed"), and bleak registers none. So the
pairing is started from a D-Bus connection that holds an `org.bluez.Agent1` (bluetoothd asks the agent of whoever called
`Pair`): it confirms only that device, and goes away with the connection.
"""

import asyncio
import contextlib
import itertools
import logging
import sys
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import OrPattern
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from majordom_integration_sdk.capabilities.ble import (
    BLEConnection,
    BLEPairingError,
    GattCharacteristic,
    GattService,
    OnAdvertisement,
)

logger = logging.getLogger(__name__)

AGENT_PATH = "/io/majordom/pairing_agent"
REJECTED = "org.bluez.Error.Rejected"
_agents = itertools.count()

Answer = tuple[str, list[Any]] | str  # (signature, body) to return, or the name of the error to raise


def agent_answer(member: str, body: list[Any], device_path: str) -> Answer:
    """What the agent answers bluetoothd's ``member`` call; anything not about ``device_path`` is refused."""
    if member in ("Release", "Cancel"):
        return "", []
    if not body or body[0] != device_path:
        return REJECTED
    if member in ("RequestAuthorization", "RequestConfirmation"):  # Just Works: nothing to compare without a display
        return "", []
    return REJECTED  # passkeys and PIN codes (BR/EDR), service authorization, displays we don't have


def device_path(adapter: str, address: str) -> str:
    return f"/org/bluez/{adapter}/dev_{address.upper().replace(':', '_')}"


async def pair_through_agent(adapter: str, address: str):
    """Pairs the connected device ``address`` on ``adapter`` (hci0, …); raises `ConnectionError` with BlueZ's error."""
    # bleak's BlueZ backend brings dbus-fast, on Linux only
    from dbus_fast import BusType, Message, MessageType  # ty: ignore[unresolved-import]
    from dbus_fast.aio import MessageBus  # ty: ignore[unresolved-import]

    path, agent = device_path(adapter, address), f"{AGENT_PATH}{next(_agents)}"

    def on_message(message: Message) -> Message | None:
        if message.message_type != MessageType.METHOD_CALL or message.path != agent:
            return None
        answer = agent_answer(message.member or "", list(message.body), path)
        if isinstance(answer, str):
            return Message.new_error(message, answer, "Not the device being paired")
        return Message.new_method_return(message, *answer)

    async def call(message: Message):
        reply = await bus.call(message)
        if reply is not None and reply.message_type == MessageType.ERROR:
            raise ConnectionError(f"{reply.error_name}: {' '.join(map(str, reply.body))}".strip(": "))

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.add_message_handler(on_message)
    try:
        manager = {"destination": "org.bluez", "path": "/org/bluez", "interface": "org.bluez.AgentManager1"}
        await call(Message(**manager, member="RegisterAgent", signature="os", body=[agent, "NoInputNoOutput"]))
        try:
            await call(Message(destination="org.bluez", path=path, interface="org.bluez.Device1", member="Pair"))
        except ConnectionError as e:
            if not str(e).startswith("org.bluez.Error.AlreadyExists"):  # paired already: the link is encrypted
                raise
        finally:
            with contextlib.suppress(ConnectionError):
                await call(Message(**manager, member="UnregisterAgent", signature="o", body=[agent]))
    finally:
        bus.disconnect()


_EVERY_ADVERTISER = [OrPattern(0, AdvertisementDataType.FLAGS, flags) for flags in (b"\x02", b"\x06", b"\x1a")]


class LocalAdapter:
    """A Bluetooth adapter of this machine, through bleak (BlueZ on Linux)."""

    is_pairable = True  # LE Secure Connections where the device offers it; through a scoped BlueZ agent on Linux

    def __init__(self, adapter: str | None = None):
        self.name = adapter or "local"
        self._options = {"bluez": {"adapter": adapter}} if adapter else {}
        self._scanner: BleakScanner | None = None
        self._devices: dict[str, BLEDevice] = {}  # address -> last seen; connecting by it skips a scan
        self.is_passive = False  # set: a passive scan of every advertiser, the listeners' matchers choose
        self._is_passive_refused = False
        self._on_advertisement: OnAdvertisement | None = None
        self._scan_lock = asyncio.Lock()  # a connect holds it: no scan starts until it ends

    async def set_scan_mode(self, is_active: bool):
        if self.is_passive == (not is_active):
            return
        self.is_passive = not is_active
        if self._scanner and self._on_advertisement:  # scanning: again, in the new mode
            await self.stop()
            await self.start(self._on_advertisement)

    async def start(self, on_advertisement: OnAdvertisement):
        self._on_advertisement = on_advertisement

        def on_detection(device: BLEDevice, adv_data: AdvertisementData):
            self._devices.pop(device.address, None)  # re-added last: the dict stays oldest first
            self._devices[device.address] = device
            if len(self._devices) > 1024:  # phones rotate their address: drop the oldest
                del self._devices[next(iter(self._devices))]
            on_advertisement(device, adv_data)

        # BlueZ refuses a passive scan without --experimental (Raspberry Pi OS's default): then it scans actively. With
        # it, a Pi 5 (BlueZ 5.82, no offload) passes adverts only through patterns on the flags (2026-10-14), as in HA.
        async with self._scan_lock:
            if self.is_passive and not self._is_passive_refused:
                bluez = {**self._options.get("bluez", {}), "or_patterns": _EVERY_ADVERTISER}
                try:  # macOS refuses a passive scan already when the scanner is made
                    self._scanner = BleakScanner(detection_callback=on_detection, scanning_mode="passive", bluez=bluez)
                    return await self._scanner.start()
                except Exception as e:
                    # BlueZ's advertisement monitor is missing (an old or non-experimental bluetoothd)
                    logger.warning(f"BLE {self.name} can't scan passively, scanning actively: {e}")
                    self._is_passive_refused = True
            self.is_passive = False
            self._scanner = BleakScanner(detection_callback=on_detection, **self._options)
            await self._scanner.start()

    async def stop(self):
        async with self._scan_lock:
            scanner, self._scanner = self._scanner, None
            if scanner:
                await scanner.stop()

    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> BLEConnection:
        device = self._devices.get(address, address)
        client = BleakClient(device, disconnected_callback=lambda _: on_disconnect(), **self._options)
        async with self._scan_lock:
            scanner = self._scanner
            if scanner:  # many adapters (the Raspberry Pi's included) stall a connect while discovering
                await scanner.stop()
            try:
                await client.connect()
            finally:
                if scanner:
                    await scanner.start()
        return _BleakConnection(client, self._options.get("bluez", {}).get("adapter") or "hci0")


class _BleakConnection:
    def __init__(self, client: BleakClient, adapter: str):
        self._client, self._adapter = client, adapter

    @property
    def mtu(self) -> int:
        # On BlueZ bleak says 23 until the MTU is acquired, though BlueZ exchanged the real one on connect and every
        # characteristic carries it; Matter's BTP sized its segments by the 23 and ran ~10x slower (2026-10-22).
        characteristics = self._client.services.characteristics.values()
        return max((c.max_write_without_response_size + 3 for c in characteristics), default=self._client.mtu_size)

    async def services(self) -> list[GattService]:
        return [
            GattService(
                service.uuid,
                service.handle,
                tuple(GattCharacteristic(c.uuid, c.handle, tuple(c.properties)) for c in service.characteristics),
            )
            for service in self._client.services
        ]

    async def read(self, handle: int) -> bytes:
        return bytes(await self._client.read_gatt_char(handle))

    async def write(self, handle: int, data: bytes, is_response_needed: bool = True):
        await self._client.write_gatt_char(handle, data, response=is_response_needed)

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        await self._client.start_notify(handle, lambda _, data: callback(bytes(data)))

    async def stop_notify(self, handle: int):
        await self._client.stop_notify(handle)

    async def pair(self):
        try:
            if sys.platform == "linux":  # bleak's pair() brings no agent: LE Secure Connections fails without one
                await pair_through_agent(self._adapter, self._client.address)
            else:  # the OS pairs on its own, with its own prompts
                await self._client.pair()
        except (ConnectionError, BleakError, EOFError) as e:  # org.bluez.Error.AuthenticationFailed, a dropped link
            raise BLEPairingError(f"Pairing failed: {e}") from e

    async def unpair(self):
        await self._client.unpair()

    async def disconnect(self):
        await self._client.disconnect()
