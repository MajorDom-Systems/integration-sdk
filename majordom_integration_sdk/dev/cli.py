"""Interactive REPL for running one integration standalone — the ``majordom-integration-sdk[cli]`` extra.

Ported from the MajorDom Hub's operator CLI and scoped to a single controller: start it with real
discovery services and a local repository, then pair / control / inspect devices from a prompt.
Where :func:`run_controller` just *watches*, this lets you *drive* the integration by hand.

Needs the ``cli`` extra (typer-shell + rich)::

    pip install "majordom-integration-sdk[cli]"

    import asyncio
    from majordom_integration_sdk.dev import run_cli
    from majordom_matter import MatterController  # your integration

    asyncio.run(run_cli(MatterController, db_path="devices.db"))
"""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import rich
from typer_shell import make_typer_shell

from ..controller.abstract_controller import AbstractController, ControllerOutput
from ..schemas.command import DeviceCommand
from ..schemas.device import CredentialsType, ProvidedCredentials
from ..schemas.event import DeviceParameterChange
from ..schemas.parameter import ParameterRole
from . import build_dependencies

logger = logging.getLogger("majordom_integration_sdk.dev")


# --- prompt-safe output ------------------------------------------------------------------------
# The catch with an interactive REPL: the shell is blocked reading a line in a worker thread, so
# anything printed from elsewhere (discoveries/events on the event loop, or a stray log line) lands
# *on the prompt line* and eats what the user is typing. Print above the prompt and restore the
# cursor instead. Ported from the Hub CLI's `println`; falls back to a plain print when stdout
# isn't a TTY (pipes, tests, logs to a file) where the escape codes would just be noise.


def _println(msg: object) -> None:
    if not sys.stdout.isatty():
        rich.print(msg)
        return
    builtins.print("\x1b[s\x1b[1A\x1b[999D\x1b[1S\x1b[L", end="", flush=True)  # save, up, home, scroll, insert-line
    rich.print(msg, end="", flush=True)
    builtins.print("\x1b[u", end="", flush=True)  # restore cursor to the prompt


def _print(*values: object, sep: str = "  ") -> None:
    """Prompt-safe print: wrap to the terminal width and emit each line via :func:`_println`."""
    columns, _ = shutil.get_terminal_size((80, 20))
    for line in sep.join(str(v) for v in values).split("\n"):
        if not line:
            _println("")
        for i in range(0, len(line), columns or 80):
            _println(line[i : i + (columns or 80)])


class _CapturingHandler(logging.Handler):
    """Buffer every log record instead of emitting it, so chatty controllers / discovery /
    matter-server logs (and warnings, via ``logging.captureWarnings``) don't disrupt the
    interactive prompt. The whole buffer is dumped to stderr once on exit or crash — quiet TUI
    during the session, full logs for debugging after."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def dump(self) -> None:
        if not self.records:
            return
        builtins.print("\n--- captured logs (CLI session) ---", file=sys.stderr, flush=True)
        for record in self.records:
            builtins.print(self.format(record), file=sys.stderr, flush=True)


# --- controller-driving core (testable without the REPL) ---------------------------------------


class _CliSession:
    """The id-based logic behind the CLI commands. Kept separate from the typer-shell wiring so it
    can be exercised directly against a fake controller in tests. Mirrors the Hub relay's object
    resolution (``repo.get`` + ``get_parameter_state`` + ``model_validate`` into the controller's
    ``device_type`` / ``parameter_type``)."""

    def __init__(self, controller: AbstractController):
        self._controller = controller
        self._deps = controller.dependencies

    @property
    def discoveries(self):
        return self._controller.discoveries

    async def pair(self, discovery_id: UUID, credentials_type: str = "none", credentials_value: str = ""):
        discovery = self._controller.discoveries.get(discovery_id)
        if not discovery:
            return None
        credentials = ProvidedCredentials(type=CredentialsType(credentials_type), value=credentials_value or None)
        return await self._controller.pair_device(discovery, credentials)

    async def pair_window(self, seconds: int) -> None:
        await self._controller.start_pairing_window(seconds)

    async def list_devices(self):
        async with self._deps.make_device_repository() as repo:
            return await repo.get_all()

    async def device_state(self, device_id: UUID):
        async with self._deps.make_device_repository() as repo:
            return await repo.state(device_id)

    async def _device(self, device_id: UUID):
        async with self._deps.make_device_repository() as repo:
            device = await repo.get(device_id)
        return self._controller.device_type.model_validate(device) if device else None

    async def identify(self, device_id: UUID) -> bool:
        if not (device := await self._device(device_id)):
            return False
        await self._controller.identify(device)
        return True

    async def fetch(self, device_id: UUID) -> bool:
        if not (device := await self._device(device_id)):
            return False
        await self._controller.fetch(device)
        return True

    async def unpair(self, device_id: UUID) -> bool:
        if not (device := await self._device(device_id)):
            return False
        await self._controller.unpair(device)
        return True

    async def control(self, device_id: UUID, parameter_id: UUID, value: object) -> str:
        command = DeviceCommand(device_id=device_id, parameter_id=parameter_id, value=value)
        async with self._deps.make_device_repository() as repo:
            device = await repo.get(command.device_id)
            if not device:
                return f"No device {device_id}"
            parameter = await repo.get_parameter_state(command.device_id, command.parameter_id)
            if not parameter:
                return f"No parameter {parameter_id} on device {device_id}"
            if parameter.role != ParameterRole.control:
                return f"Parameter {parameter_id} is not controllable (role={parameter.role.value})"
            await self._controller.send_command(
                command,
                self._controller.device_type.model_validate(device),
                self._controller.parameter_type.model_validate(parameter),
            )
        return "Command sent"


class _CliControllerOutput(ControllerOutput):
    """The CLI's own output delegate — a different approach from the logging one: it shows each
    callback through the prompt-safe printer (intentional TUI content, not logs) *and* persists
    state to the repository, so ``devices`` / ``device`` reflect live availability and values. A
    single-controller stand-in for the Hub relay's output side."""

    def __init__(self, make_device_repository):
        self._make_device_repository = make_device_repository

    async def controller_did_receive_discovery(self, controller, discovery):
        _print(f"[discovered] {discovery.id}  {discovery.device_name}  ({discovery.transport})")

    async def controller_did_update_discovery(self, controller, discovery):
        _print(f"[discovery updated] {discovery.id}")

    async def controller_did_lose_discovery(self, controller, discovery_id):
        _print(f"[discovery lost] {discovery_id}")

    async def controller_did_connect_device(self, controller, device_id):
        _print(f"[connected] {device_id}")
        async with self._make_device_repository() as repo:
            if device := await repo.get(device_id):
                device.last_seen = datetime.now()
                device.available = True
                await repo.save(device)

    async def controller_did_lose_device(self, controller, device_id):
        _print(f"[lost] {device_id}")
        async with self._make_device_repository() as repo:
            if device := await repo.get(device_id):
                device.available = False
                device.last_error = f"Device is no longer connected to the {controller.name} network"
                await repo.save(device)

    async def controller_did_receive_events(self, controller, events):
        events = list(events)
        async with self._make_device_repository() as repo:
            for event in events:
                if not isinstance(event, DeviceParameterChange):
                    continue
                if device := await repo.get(event.device_id):
                    device.last_seen = datetime.now()
                    await repo.save(device)
                if state := await repo.get_parameter_state(event.device_id, event.parameter_id):
                    state.value = event.value
                    await repo.save_parameter_state(event.device_id, state)
        for event in events:
            _print(f"[event] {type(event).__name__}: {event!r}")


# --- the REPL ----------------------------------------------------------------------------------


async def run_cli(
    controller_type: type[AbstractController],
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Start ``controller_type`` (with real discovery + a local repository) and drop into an
    interactive prompt. ``db_path`` file-backs the repository so paired devices survive restarts;
    omit it for in-memory. ``exit`` / Ctrl-D leaves the prompt and stops everything cleanly."""
    # Capture all logs + warnings into a buffer for the duration of the session so they don't break
    # the interactive TUI; the buffer is dumped to stderr on exit/crash (see the finally block).
    # The CLI's own discovery/event/command output (via _print) stays visible and prompt-safe.
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    captured = _CapturingHandler()
    captured.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.handlers[:] = [captured]
    root.setLevel(logging.DEBUG)
    logging.captureWarnings(True)

    loop = asyncio.get_running_loop()

    # Identity is class-level, so the repository/documents folder are scoped before construction;
    # then swap in an output that also persists state.
    deps = build_dependencies(
        storage_root=storage_root,
        db_path=db_path,
        integration=controller_type.name,
        slug=controller_type.slug(),
    )
    deps = deps.copy(output=_CliControllerOutput(deps.make_device_repository))
    controller = controller_type(deps)
    session = _CliSession(controller)

    await deps.zeroconf_discovery_service.start()
    await deps.ssdp_discovery_service.start()
    await deps.ble_discovery_service.start()
    await controller.start()
    _print(f"{controller.name} started — discovering. Type 'help' for commands, 'exit' to quit.")

    # Shell commands run in a worker thread (typer-shell blocks); hop back onto this loop to call
    # the controller's async API.
    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    app = make_typer_shell(
        prompt=f"{controller.name.lower()} > ",
        intro=f"MajorDom {controller.name} standalone CLI. Type 'help' for commands, 'exit' to quit.",
    )

    @app.command()
    def discoveries():
        """List discoveries currently visible (id, name, transport, expected credentials)."""
        found = session.discoveries
        if not found:
            _print("(no discoveries yet)")
        for id, d in found.items():
            expects = ", ".join(c.value for c in d.expected_credentials_options)
            _print(f"{id}  {d.device_name}  transport={d.transport}  expects=[{expects}]")

    @app.command()
    def discovery(discovery_id: str):
        """Show full discovery info by id."""
        d = session.discoveries.get(UUID(discovery_id))
        _print(d.model_dump_json(indent=2) if d else f"No discovery {discovery_id}")

    @app.command()
    def pair(discovery_id: str, credentials_type: str = "none", credentials_value: str = ""):
        """Pair a discovered device. credentials_type must be one of the discovery's expected options
        (see `discovery`); credentials_value is the code/QR string when that type needs one."""
        device_id = call(session.pair(UUID(discovery_id), credentials_type, credentials_value))
        _print(f"Paired device {device_id}" if device_id else f"No discovery {discovery_id}")

    @app.command()
    def pair_window(seconds: int = 60):
        """Open a pairing/join window for `seconds` (Zigbee needs this; a no-op for integrations
        that discover continuously)."""
        call(session.pair_window(seconds))
        _print(f"Pairing window open for {seconds}s")

    @app.command()
    def devices():
        """List paired devices (id, name, available)."""
        found = call(session.list_devices())
        if not found:
            _print("(no paired devices)")
        for device in found:
            _print(f"{device.id}  {device.name}  available={device.available}")

    @app.command()
    def device(device_id: str):
        """List a paired device's parameters (id, name, role, visibility, value)."""
        state = call(session.device_state(UUID(device_id)))
        if not state:
            _print(f"No device {device_id}")
            return
        for p in state.parameters:
            _print(f"{p.id}  {p.name}  role={p.role.value}  visibility={p.visibility.value}  value={p.value!r}")

    @app.command()
    def identify(device_id: str):
        """Ask a device to identify itself (blink/beep)."""
        _print("identify sent" if call(session.identify(UUID(device_id))) else f"No device {device_id}")

    @app.command()
    def fetch(device_id: str):
        """Refresh a device's state from the device."""
        _print("fetched" if call(session.fetch(UUID(device_id))) else f"No device {device_id}")

    @app.command()
    def unpair(device_id: str):
        """Remove a paired device."""
        _print(f"Unpaired {device_id}" if call(session.unpair(UUID(device_id))) else f"No device {device_id}")

    @app.command()
    def control(device_id: str, parameter_id: str, value: str):
        """Send a command: device_id parameter_id value (value is parsed as JSON when possible, else
        used as a raw string). The target parameter must have the `control` role."""
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        _print(call(session.control(UUID(device_id), UUID(parameter_id), parsed)))

    # typer-shell parses sys.argv on start; clear it so a bare `run_cli()` doesn't inherit flags.
    sys.argv = sys.argv[:1]
    try:
        await loop.run_in_executor(None, app)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await controller.stop()
        await deps.ble_discovery_service.stop()
        await deps.ssdp_discovery_service.stop()
        await deps.zeroconf_discovery_service.stop()
        _print(f"{controller.name} stopped.")
        # restore logging and dump everything captured during the session for debugging
        logging.captureWarnings(False)
        root.handlers[:], root.level = saved_handlers, saved_level
        captured.dump()


__all__ = ["run_cli"]
