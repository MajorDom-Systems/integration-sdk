"""Shared fixtures for the SDK's own test suite."""

import pytest
from factories import make_device_state

from majordom_integration_sdk.schemas.device import DeviceState


@pytest.fixture
def device_state() -> DeviceState:
    return make_device_state()
