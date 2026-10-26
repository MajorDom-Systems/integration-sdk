"""Run one integration controller standalone — no Hub process.

This is the "run standalone, then bridge into the MajorDom language" half of the
integration workflow: the runtime (the ``runtime`` extra) runs the controller among the SDK's
`default_peripherals()` as under the Hub — the radios it finds, the Bluetooth router over the local
adapters, DNS-SD, SSDP and the containers it runs — and starts it once its needs are bound, then
you watch what it discovers/reports on the console.

    import asyncio
    import majordom_hue  # your integration
    from majordom_integration_sdk.dev import run_controller

    asyncio.run(run_controller(majordom_hue.peripherals))  # its controller and the servers it needs

Without Docker, ``pinned=[Endpoint(...)]`` gives the endpoints of the servers you run yourself;
``runtime_peripherals=[HostNetwork(), ...]`` runs only those around it (no USB scanning);
``peripherals={...}`` skips the runtime altogether (library mode).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from majordom_integration_sdk.controller.abstract_controller import AbstractController
from majordom_integration_sdk.controller.output import ControllerOutput, LoggingControllerOutput
from majordom_integration_sdk.controller.platform import Platform
from majordom_integration_sdk.peripherals import Bound, Endpoint, Peripheral, Peripherals
from majordom_integration_sdk.repository.sqlite import SqliteDeviceRepository

if TYPE_CHECKING:
    from majordom_integration_sdk.runtime import PeripheralStatus, Runtime

logger = logging.getLogger("majordom_integration_sdk.dev")


def build_platform(*, storage_root: Path | None = None, db_path: str | Path | None = None) -> Platform:
    """Assemble the host for a standalone run, as the Hub gives it to every peripheral.

    ``db_path`` selects a file-backed :class:`SqliteDeviceRepository` (state survives
    restarts); omit it for an in-memory repository. The storage root, where each peripheral
    gets its documents folder, defaults to ``./.majordom-dev``.
    """
    root = Path(storage_root) if storage_root is not None else Path(".majordom-dev")
    root.mkdir(parents=True, exist_ok=True)
    platform = Platform(documents_root=root)
    if db_path is not None:
        return replace(platform, make_device_repository=lambda key: SqliteDeviceRepository(db_path, key).session())
    return platform


class LoggingRuntimeOutput:
    """A ``RuntimeOutput`` that logs every change of status: why a peripheral waits or failed."""

    def peripheral_did_change_status(self, peripheral: Peripheral, status: PeripheralStatus):
        logger.info(f"{peripheral.key} {status}")

    def peripheral_did_learn(self, peripheral: Peripheral, facts: Mapping[str, Any]):
        logger.info(f"{peripheral.key} learned {', '.join(facts)}")  # the runtime's platform took them

    def peripheral_did_disappear(self, peripheral: Peripheral):
        logger.info(f"{peripheral.key} is gone")


def build_runtime(
    integration: Sequence[Peripheral],
    data: Path,
    *,
    runtime_peripherals: Sequence[Peripheral] | None = None,
    pinned: Sequence[Endpoint] = (),
    platform: Platform | None = None,
    controller_output: ControllerOutput | None = None,
) -> Runtime:
    """The runtime for one integration, as under the Hub: the endpoints you pin first, then ``runtime_peripherals``
    (`default_peripherals()` by default), then what the integration exports, all on ``platform`` (the one its
    controller was given), what its controller reports going to ``controller_output`` (logged by default). Needs the
    ``runtime`` extra."""
    if importlib.util.find_spec("serial") is None:
        raise ImportError(
            "Resolving needs takes the runtime — install it with:\n"
            '    pip install "majordom-integration-sdk[runtime,ble,discovery]"\n'
            "or pass peripherals={...} yourself."
        )
    from majordom_integration_sdk.runtime import Pinned, Runtime, default_peripherals

    around = default_peripherals() if runtime_peripherals is None else runtime_peripherals
    peripherals = [Pinned(*pinned), *around, *integration]
    return Runtime(
        data, peripherals, platform=platform, controller_output=controller_output, output=LoggingRuntimeOutput()
    )


class Runner:
    """Runs one controller standalone: under ``runtime``, which starts it once its needs are bound and hands moved
    peripherals, settings and platform facts over as under the Hub; or, without, with the needs bound by hand."""

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
    *,
    storage_root: Path | None = None,
    db_path: str | Path | None = None,
    peripherals: Mapping[str, Bound] | None = None,
    runtime_peripherals: Sequence[Peripheral] | None = None,
    pinned: Sequence[Endpoint] = (),
    **platform: Any,
):
    """Start what ``integration`` exports (its ``peripherals``: the controller and the servers it needs) under the
    runtime, or only its controller with the ``peripherals`` you bind, and run until interrupted (Ctrl-C), then stop
    everything cleanly. It is given its platform as by the Hub, ``platform`` overriding fields of `Platform`, e.g.
    ``region``; ``runtime_peripherals`` and ``pinned`` go to the runtime (`build_runtime`)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    exported = integration(replace(build_platform(storage_root=storage_root, db_path=db_path), **platform))
    runner = _runner(exported, storage_root, peripherals, runtime_peripherals, pinned, LoggingControllerOutput())
    await runner.start()
    try:
        await asyncio.Event().wait()  # run forever until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.stop()
        logger.info(f"{runner.controller.name} stopped.")


def _runner(
    exported: Sequence[Peripheral],
    storage_root: Path | None,
    peripherals: Mapping[str, Bound] | None,
    runtime_peripherals: Sequence[Peripheral] | None,
    pinned: Sequence[Endpoint],
    controller_output: ControllerOutput,
) -> Runner:
    """What an integration exported, run around its one controller, which reports to ``controller_output``."""
    controllers = [peripheral for peripheral in exported if isinstance(peripheral, AbstractController)]
    if len(controllers) != 1:
        raise ValueError(f"An integration exports one controller, not {len(controllers)}: {list(exported)}")
    controller, root = controllers[0], Path(storage_root) if storage_root is not None else Path(".majordom-dev")
    if peripherals is not None:
        controller.peripherals, controller.output = Peripherals(peripherals), controller_output
        return Runner(controller)
    runtime = build_runtime(
        exported,
        root / "runtime",
        runtime_peripherals=runtime_peripherals,
        pinned=pinned,
        platform=controller.platform,
        controller_output=controller_output,
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
