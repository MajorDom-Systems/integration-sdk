"""Run one integration controller standalone — no Hub process.

This is the "run standalone, then bridge into the MajorDom language" half of the
integration workflow: wire a single controller with real discovery services and a local
repository, start it, and watch what it discovers/reports on the console. Modeled on the
discovery services' own ``__main__`` demos rather than the Hub's multi-controller CLI.

    import asyncio
    from majordom_integration_sdk.dev import run_controller
    from majordom_hue import HueController  # your integration

    asyncio.run(run_controller(HueController))
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from majordom_integration_sdk.controller.abstract_controller import AbstractController, ControllerOutput
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryService
from majordom_integration_sdk.discovery.ssdp_discovery import SSDPDiscoveryService
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryService
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.repository.sqlite import SqliteDeviceRepository
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

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

    async def controller_did_encounter_error(
        self,
        controller: AbstractController,
        message: str,
        still_running: bool,
    ):
        state = "still running" if still_running else "STOPPED — integration inactive"
        logger.error("integration error (%s): %s", state, message)

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        logger.info(
            "notification [%s/%s]: %s", notification.type.value, notification.priority.value, notification.message
        )


def build_dependencies(
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
    integration: str | None = None,
    slug: str | None = None,
    output: ControllerOutput | None = None,
) -> AbstractController.Dependencies:
    """Assemble real (network-live) dependencies for a standalone run.

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
    return AbstractController.Dependencies(
        output=output or LoggingControllerOutput(),
        make_device_repository=repository.session,
        documents_folder=folder,
        zeroconf_discovery_service=ZeroconfDiscoveryService(),
        ssdp_discovery_service=SSDPDiscoveryService(),
        ble_discovery_service=BLEDiscoveryService(),
    )


async def run_controller(
    controller_type: type[AbstractController],
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Instantiate ``controller_type``, start it and its discovery services, and run until
    interrupted (Ctrl-C), then stop everything cleanly."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # The integration's identity is class-level, so its repository and documents folder can be
    # scoped before it's constructed.
    deps = build_dependencies(
        storage_root=storage_root,
        db_path=db_path,
        integration=controller_type.name,
        slug=controller_type.slug(),
    )
    controller = controller_type(deps)

    await deps.zeroconf_discovery_service.start()
    await deps.ssdp_discovery_service.start()
    await deps.ble_discovery_service.start()
    await controller.start()
    logger.info("%s started — watching for devices. Ctrl-C to stop.", controller.name)

    try:
        await asyncio.Event().wait()  # run forever until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await controller.stop()
        await deps.ble_discovery_service.stop()
        await deps.ssdp_discovery_service.stop()
        await deps.zeroconf_discovery_service.stop()
        logger.info("%s stopped.", controller.name)


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


__all__ = ["run_controller", "run_cli", "build_dependencies", "LoggingControllerOutput"]
