"""Shared fixtures + factories for the SDK's own test suite."""

from uuid import UUID, uuid4

import pytest

from majordom_integration_sdk.schemas.device import DeviceState
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterVisibility,
)


def make_parameter_state(
    *,
    id: UUID | None = None,
    name: str = "Power",
    data_type: ParameterDataType = ParameterDataType.bool,
    role: ParameterRole = ParameterRole.control,
    visibility: ParameterVisibility = ParameterVisibility.user,
) -> ParameterState:
    state = ParameterState(
        id=id or uuid4(),
        name=name,
        data_type=data_type,
        role=role,
        visibility=visibility,
        integration_data=None,
    )
    if data_type is ParameterDataType.bool:
        state = state.with_value(True)
    return state


def make_device_state(
    *,
    id: UUID | None = None,
    integration: str = "example",
    name: str = "Lamp",
    parameters: list[ParameterState] | None = None,
) -> DeviceState:
    return DeviceState(
        id=id or uuid4(),
        name=name,
        room_id=uuid4(),
        transport="wifi",
        integration=integration,
        manufacturer="ACME",
        parameters=parameters if parameters is not None else [make_parameter_state()],
    )


@pytest.fixture
def device_state() -> DeviceState:
    return make_device_state()
