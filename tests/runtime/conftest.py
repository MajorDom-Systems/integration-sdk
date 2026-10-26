import pytest
from support import STARTED

from majordom_integration_sdk.runtime import runtime as module
from majordom_integration_sdk.runtime.peripherals.services import builtin, compose


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(module, "RETRY_DELAYS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(compose, "READY_POLL", 0.01)
    monkeypatch.setattr(builtin, "POLL", 0.01)


@pytest.fixture(autouse=True)
async def stopped():
    yield
    while STARTED:
        await STARTED.pop().stop()
