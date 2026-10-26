"""Driving a runtime in tests: run it, wait for what it started, tell it what changed."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from majordom_integration_sdk import Peripheral
from majordom_integration_sdk.runtime import PeripheralState, Runtime


async def settle(runtime: Runtime):
    """Let every start in flight finish, and the runtime act on it."""
    while pending := [task for task in runtime._tasks if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


async def until(runtime: Runtime, condition: Callable[[], bool], timeout: float = 2, settled: bool = True):
    """Wait for what a timer does, e.g. a retry; then, if ``settled``, for what it set off."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)
    if settled:
        await settle(runtime)


STARTED: list[Runtime] = []  # stopped after each test


async def run(path: Path, *peripherals: Peripheral, settled: bool = True, **kwargs) -> Runtime:
    """A runtime of ``peripherals``, started; with ``settled``, once every start it set off returned."""
    runtime = Runtime(path / "data", peripherals, **kwargs)
    STARTED.append(runtime)
    await runtime.start()
    if settled:
        await settle(runtime)
    return runtime


async def changed(runtime: Runtime, peripheral: Peripheral):
    """``peripheral`` changed what it provides or brings along on its own, and told the runtime."""
    await peripheral.output.peripheral_did_change_provided(peripheral)
    await settle(runtime)


def state(runtime: Runtime, peripheral: Peripheral) -> PeripheralState:
    return runtime.status(peripheral).state
