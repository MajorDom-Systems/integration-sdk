"""Run one integration controller standalone — no Hub process.

This is the "run standalone, then bridge into the MajorDom language" half of the
integration workflow: the runtime (the ``runtime`` extra) runs the controller among the SDK's
`default_peripherals()` as under the Hub — the radios it finds, the Bluetooth router over the local
adapters, DNS-SD, SSDP and the containers it runs — and starts it once its needs are bound, then
you watch what it discovers/reports on the console.

    import asyncio
    import majordom_hue  # your integration
    from majordom_integration_sdk.dev import run_controller

    asyncio.run(run_controller(majordom_hue.peripherals, majordom_hue.HueController))

Without Docker, ``pinned=[Endpoint(...)]`` gives the endpoints of the servers you run yourself;
``runtime_peripherals=[HostNetwork(), ...]`` runs only those around it (no USB scanning);
``peripherals={...}`` skips the runtime altogether (library mode).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from majordom_integration_sdk.controller.abstract_controller import AbstractController
from majordom_integration_sdk.controller.output import ControllerOutput, IntegrationError
from majordom_integration_sdk.controller.platform import Context, Platform
from majordom_integration_sdk.peripherals import Bound, Endpoint, Peripheral, Peripherals
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.repository.protocol import DeviceRepositoryProtocol
from majordom_integration_sdk.repository.sqlite import SqliteDeviceRepository
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.event import Event
from majordom_integration_sdk.schemas.notification import Notification

if TYPE_CHECKING:
    from majordom_integration_sdk.runtime import PeripheralStatus, Runtime

logger = logging.getLogger("majordom_integration_sdk.dev")


class LoggingControllerOutput(ControllerOutput):
    """A ``ControllerOutput`` that logs every callback — the Hub's stand-in for dev runs."""

    async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info(f"discovered {discovery.device_name} ({discovery.transport}) id={discovery.id}")

    async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery):
        logger.info(f"updated discovery {discovery.id}")

    async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID):
        logger.info(f"lost discovery {discovery_id}")

    async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID):
        logger.info(f"connected device {device_id}")

    async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
        logger.info(f"lost device {device_id}")

    async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]):
        for event in events:
            logger.info(f"event {type(event).__name__}: {event!r}")

    async def controller_did_encounter_error(self, controller: AbstractController, error: IntegrationError):
        logger.error(f"integration error {error.id}: {error.message}")

    async def controller_did_resolve_error(self, controller: AbstractController, error_id: str):
        logger.info(f"integration error {error_id} resolved")

    async def controller_did_emit_notification(self, controller: AbstractController, notification: Notification):
        logger.info(f"notification [{notification.type.value}/{notification.priority.value}]: {notification.message}")


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


class LoggingRuntimeOutput:
    """A ``RuntimeOutput`` that logs every change of status: why a peripheral waits or failed."""

    def peripheral_did_change_status(self, peripheral: Peripheral, status: PeripheralStatus):
        logger.info(f"{peripheral.name} {status}")

    def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        logger.info(f"{peripheral.name} learned {', '.join(facts)}")

    def peripheral_did_disappear(self, peripheral: Peripheral):
        logger.info(f"{peripheral.name} is gone")


def build_runtime(
    integration: Sequence[Peripheral],
    data: Path,
    *,
    runtime_peripherals: Sequence[Peripheral] | None = None,
    pinned: Sequence[Endpoint] = (),
    context: Callable[[], Context] = Context,
) -> Runtime:
    """The runtime for one integration, as under the Hub: the endpoints you pin first, then ``runtime_peripherals``
    (`default_peripherals()` by default), then what the integration exports. Needs the ``runtime`` extra."""
    if importlib.util.find_spec("serial") is None:
        raise ImportError(
            "Resolving needs takes the runtime — install it with:\n"
            '    pip install "majordom-integration-sdk[runtime,ble,discovery]"\n'
            "or pass peripherals={...} yourself."
        )
    from majordom_integration_sdk.runtime import Pinned, Runtime, default_peripherals

    around = default_peripherals() if runtime_peripherals is None else runtime_peripherals
    return Runtime(data, [Pinned(*pinned), *around, *integration], context=context, output=LoggingRuntimeOutput())


class Runner:
    """Runs one controller standalone: under ``runtime``, which starts it once its needs are bound and hands moved
    peripherals, settings and context over as under the Hub; or, without, with the needs bound by hand."""

    def __init__(self, controller: AbstractController, runtime: Runtime | None = None):
        self.controller, self.runtime = controller, runtime
        self._is_started = False

    @property
    def is_running(self) -> bool:
        if self.runtime is None:
            return self._is_started
        from majordom_integration_sdk.runtime import PeripheralState

        return self.runtime.status(self.controller).state == PeripheralState.RUNNING

    async def start(self):
        if self.runtime:
            await self.runtime.start()
            return
        await self.controller.start()
        self._is_started = True
        logger.info(f"{self.controller.name} started — watching for devices.")

    async def stop(self):
        if self.runtime:
            await self.runtime.stop()
        elif self._is_started:
            await self.controller.stop()
            self._is_started = False


type Integration = Callable[[Platform], Sequence[Peripheral]]  # what an integration package exports: peripherals


async def run_controller(
    integration: Integration,
    controller: type[AbstractController],
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
    peripherals: Mapping[str, Bound] | None = None,
    runtime_peripherals: Sequence[Peripheral] | None = None,
    pinned: Sequence[Endpoint] = (),
    **platform: Any,
):
    """Start what ``integration`` exports (its ``peripherals``: the ``controller`` and the servers it needs) under the
    runtime, or only its controller with the ``peripherals`` you bind, and run until interrupted (Ctrl-C), then stop
    everything cleanly. It is given its platform as by the Hub, ``platform`` overriding fields of `Platform`, e.g.
    ``context``; ``runtime_peripherals`` and ``pinned`` go to the runtime (`build_runtime`)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    exported = integration(_platform(controller, storage_root, db_path).copy(**platform))
    runner = _runner(exported, controller, storage_root, peripherals, runtime_peripherals, pinned)
    await runner.start()
    try:
        await asyncio.Event().wait()  # run forever until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.stop()
        logger.info(f"{runner.controller.name} stopped.")


def _platform(controller: type[AbstractController], storage_root: Path | None, db_path: str | Path | None) -> Platform:
    """The platform the Hub gives the integration of ``controller``: a repository and documents folder of its own."""
    return build_platform(
        storage_root=storage_root, db_path=db_path, integration=controller.name, slug=controller.slug()
    )


def _runner[C: AbstractController](
    exported: Sequence[Peripheral],
    kind: type[C],
    storage_root: Path | None,
    peripherals: Mapping[str, Bound] | None,
    runtime_peripherals: Sequence[Peripheral] | None,
    pinned: Sequence[Endpoint],
) -> Runner:
    """What an integration exported, run around its one controller of ``kind``."""
    controllers = [peripheral for peripheral in exported if isinstance(peripheral, kind)]
    if len(controllers) != 1:
        raise ValueError(f"An integration exports one {kind.__name__}, not {len(controllers)}: {list(exported)}")
    controller, root = controllers[0], Path(storage_root) if storage_root is not None else Path(".majordom-dev")
    if peripherals is not None:
        controller.peripherals = Peripherals(peripherals)
        return Runner(controller)
    runtime = build_runtime(
        exported,
        root / "runtime",
        runtime_peripherals=runtime_peripherals,
        pinned=pinned,
        context=lambda: controller.platform.context,
    )
    return Runner(controller, runtime)


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


__all__ = [
    "run_controller",
    "run_cli",
    "build_platform",
    "build_runtime",
    "Runner",
    "LoggingControllerOutput",
    "LoggingRuntimeOutput",
]
