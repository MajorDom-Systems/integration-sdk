"""Pairing through BlueZ with an agent of our own, scoped to the one device being paired.

LE Secure Connections "Just Works" still asks the host to confirm (the kernel's confirm hint), and bluetoothd turns that
into an agent call: without an agent the pairing fails ("Numeric comparison failed"), and bleak registers none. So the
pairing is started from a D-Bus connection that holds an `org.bluez.Agent1` (bluetoothd asks the agent of whoever called
`Pair`): it confirms only that device, and goes away with the connection.
"""

import contextlib
import itertools
from typing import Any

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


async def pair(adapter: str, address: str) -> None:
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

    async def call(message: Message) -> None:
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
