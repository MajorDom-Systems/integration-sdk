"""Run one integration controller standalone — no Hub process.

This is the "run standalone, then bridge into the MajorDom language" half of the
integration workflow: the runtime (the ``runtime`` extra) binds the controller's needs as under
the Hub — the Bluetooth router over the local adapters, DNS-SD, SSDP, the radios it finds and the
services it runs — and the controller starts with its ``peripherals`` and ``platform``, then you
watch what it discovers/reports on the console.

    import asyncio
    from majordom_integration_sdk.dev import run_controller
    from majordom_hue import HueController  # your integration

    asyncio.run(run_controller(HueController))

Without Docker or radios, ``is_docker_enabled=False, is_scan_enabled=False`` and
``pinned={"zwave": Endpoint(...)}`` bind only what you give; ``peripherals={...}`` skips the
runtime altogether (library mode).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from majordom_integration_sdk.controller.abstract_controller import AbstractController
from majordom_integration_sdk.controller.output import ControllerOutput, IntegrationError
from majordom_integration_sdk.controller.platform import Platform
from majordom_integration_sdk.peripherals import Bound, Endpoint, PeripheralChange, Peripherals
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.repository.sqlite import SqliteDeviceRepository
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.runtime import Runtime

logger = logging.getLogger("majordom_integration_sdk.dev")


class LoggingControllerOutput(ControllerOutput):
    """A ``ControllerOutput`` that logs every callback — the Hub's stand-in for dev runs."""

    async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info("discovered %s (%s) id=%s", discovery.device_name, discovery.transport, discovery.id)

    async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info("updated discovery %s", discovery.id)

    async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID):
        logger.info("lost discovery %s", discovery_id)

    async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID):
        logger.info("connected device %s", device_id)

    async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
        logger.info("lost device %s", device_id)

    async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]):
        for event in events:
            logger.info("event %s: %r", type(event).__name__, event)

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError) -> None:
        state = "still running" if error.is_still_running else "STOPPED — integration inactive"
        logger.error("integration error %s (%s): %s", error.id, state, error.message)

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str) -> None:
        logger.info("integration error %s resolved", error_id)

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        logger.info(
            "notification [%s/%s]: %s", notification.type.value, notification.priority.value, notification.message
        )


def build_platform(
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
    integration: str | None = None,
    slug: str | None = None,
    output: ControllerOutput | None = None,
) -> Platform:
    """Assemble the app's services for a standalone run.

    ``db_path`` selects a file-backed :class:`SqliteDeviceRepository` (state survives
    restarts); omit it for an in-memory repository. ``integration`` scopes the repository to
    that integration's devices and gives it its own ``documents_folder`` subtree. The storage
    root defaults to ``./.majordom-dev``.
    """
    repository: DeviceRepositoryProtocol
    repository = (
        SqliteDeviceRepository(db_path, integration=integration)
        if db_path is not None
        else DeviceRepositoryMemory(integration=integration)
    )
    root = Path(storage_root) if storage_root is not None else Path(".majordom-dev")
    subfolder = slug or integration
    folder = root / subfolder if subfolder else root
    folder.mkdir(parents=True, exist_ok=True)
    return Platform(
        output=output or LoggingControllerOutput(),
        make_device_repository=repository.session,
        documents_folder=folder,
    )


def build_runtime(
    controller_type: type[AbstractController],
    data: Path,
    *,
    is_docker_enabled: bool = True,
    is_scan_enabled: bool = True,
    pinned: Mapping[str, Endpoint] = {},
    **runtime: Any,
) -> Runtime:
    """The runtime for one controller: its needs bound like under the Hub. Needs the ``runtime`` extra."""
    if importlib.util.find_spec("serial") is None:
        raise ImportError(
            "Resolving needs takes the runtime — install it with:\n"
            '    pip install "majordom-integration-sdk[runtime,ble,discovery]"\n'
            "or pass peripherals={...} yourself."
        )
    from majordom_integration_sdk.runtime import Runtime

    return Runtime(
        data,
        {controller_type.slug(): controller_type},
        is_docker_enabled=is_docker_enabled,
        is_scan_enabled=is_scan_enabled,
        pinned=pinned,
        **runtime,
    )


class Runner:
    """Drives one controller as the Hub does: starts it once its required needs are bound, stops it when one is lost,
    and hands moved peripherals, settings and context over — restarting it unless it handles them itself."""

    def __init__(
        self,
        controller_type: type[AbstractController],
        platform: Platform,
        runtime: Runtime | None = None,
        peripherals: Mapping[str, Bound] | None = None,
    ):
        self.slug, self.base, self.runtime = controller_type.slug(), platform, runtime
        self.controller = controller_type(platform)
        self.controller.peripherals = Peripherals(peripherals or {})
        self.is_running = False
        self._lock = asyncio.Lock()
        if runtime:
            runtime.on_change = self.sync

    async def start(self):
        if self.runtime:
            await self.runtime.start()  # binds, then syncs through on_change
        else:
            await self._start()

    async def stop(self):
        if self.is_running:
            await self.controller.stop()
            self.is_running = False
        if self.runtime:
            await self.runtime.stop()

    async def sync(self):
        assert self.runtime
        async with self._lock:
            peripherals = self.runtime.peripherals(self.slug)
            if peripherals is None:
                for family, reason in self.runtime.unbound(self.slug).items():
                    logger.warning("%s isn't bound: %s", family, reason)
                if self.is_running:
                    await self.controller.stop()
                    self.is_running = False
                return
            platform = self.runtime.platform(self.slug, self.base)
            before, current = self.controller.peripherals, self.controller.platform
            self.controller.peripherals, self.controller.platform = peripherals, platform
            if not self.is_running:
                return await self._start()
            handled = True
            for family in sorted(before.keys() | peripherals.keys()):
                if before.get(family) != peripherals.get(family):
                    change = PeripheralChange(family, before.get(family), peripherals.get(family))
                    handled = await self.controller.peripheral_did_change(change) and handled
            if platform.settings != current.settings:
                handled = await self.controller.settings_did_change(current.settings) and handled
            if platform.context != current.context:
                handled = await self.controller.context_did_change(current.context) and handled
            if not handled:
                await self.controller.stop()
                await self.controller.start()

    async def _start(self):
        await self.controller.start()
        self.is_running = True
        logger.info("%s started — watching for devices.", self.controller.name)


async def run_controller(
    controller_type: type[AbstractController],
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
    peripherals: Mapping[str, Bound] | None = None,
    is_docker_enabled: bool = True,
    is_scan_enabled: bool = True,
    pinned: Mapping[str, Endpoint] = {},
    **platform: Any,
) -> None:
    """Start ``controller_type`` under the runtime (or with ``peripherals`` you give) and run until interrupted
    (Ctrl-C), then stop everything cleanly. ``platform`` overrides fields of `Platform`, e.g. ``settings``;
    ``is_docker_enabled``, ``is_scan_enabled`` and ``pinned`` go to the runtime (`build_runtime`)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    runner = _runner(
        controller_type, storage_root, db_path, peripherals, is_docker_enabled, is_scan_enabled, pinned, None, platform
    )
    await runner.start()
    try:
        await asyncio.Event().wait()  # run forever until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.stop()
        logger.info("%s stopped.", controller_type.name)


def _runner(
    controller_type: type[AbstractController],
    storage_root: Path | None,
    db_path: str | Path | None,
    peripherals: Mapping[str, Bound] | None,
    is_docker_enabled: bool,
    is_scan_enabled: bool,
    pinned: Mapping[str, Endpoint],
    output: ControllerOutput | None,
    overrides: Mapping[str, Any],
) -> Runner:
    # The integration's identity is class-level, so its repository and documents folder can be
    # scoped before it's constructed.
    root = Path(storage_root) if storage_root is not None else Path(".majordom-dev")
    base = build_platform(
        storage_root=root, db_path=db_path, integration=controller_type.name, slug=controller_type.slug()
    )
    base = base.copy(output=output or base.output, **overrides)
    if peripherals is not None:
        return Runner(controller_type, base, peripherals=Peripherals(peripherals))
    runtime = build_runtime(
        controller_type,
        root / "runtime",
        is_docker_enabled=is_docker_enabled,
        is_scan_enabled=is_scan_enabled,
        pinned=pinned,
    )
    return Runner(controller_type, base, runtime)


def __getattr__(name: str):
    # `run_cli` (interactive REPL) needs the optional `cli` extra — typer-shell + rich. Import it
    # lazily so `dev` (and `run_controller`) stay usable without those dependencies installed.
    if name == "run_cli":
        try:
            from .cli import run_cli
        except ImportError as exc:  # pragma: no cover - only when the extra is missing
            raise ImportError(
                "The interactive CLI needs extra dependencies — install them with:\n"
                '    pip install "majordom-integration-sdk[cli]"'
            ) from exc
        return run_cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["run_controller", "run_cli", "build_platform", "build_runtime", "Runner", "LoggingControllerOutput"]
