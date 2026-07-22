from uuid import uuid4

from majordom_integration_sdk.parameter_audit import audit_device_parameters
from majordom_integration_sdk.schemas.parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)


def _p(name, *, vis=ParameterVisibility.user, unit=ParameterUnit.plain, role=ParameterRole.sensor):
    return Parameter(
        id=uuid4(),
        name=name,
        data_type=ParameterDataType.integer,
        unit=unit,
        role=role,
        visibility=vis,
        integration_data=None,
    )


def test_clean_device_has_no_warnings():
    params = [
        _p("temperature", unit=ParameterUnit.celsius),
        _p("humidity", unit=ParameterUnit.percentage),
        _p("battery", unit=ParameterUnit.percentage, role=ParameterRole.sensor),
    ]
    assert audit_device_parameters("Sensor", params) == []


def test_flags_over_exposure():
    params = [_p(f"attr_{i}") for i in range(12)]
    warnings = audit_device_parameters("Flooded", params)
    assert any("over-exposed" in w for w in warnings)


def test_ignores_non_user_params_in_count():
    params = [_p(f"attr_{i}", vis=ParameterVisibility.system) for i in range(20)] + [_p("on_off")]
    assert audit_device_parameters("Lean", params) == []


def test_flags_near_duplicate_names():
    params = [_p("current_x", unit=ParameterUnit.percentage), _p("current_y", unit=ParameterUnit.percentage)]
    assert any("near-duplicate" in w for w in audit_device_parameters("Light", params))


def test_similar_name_whitelist_suppresses():
    params = [
        _p("occupied_heating_setpoint", unit=ParameterUnit.celsius, role=ParameterRole.control),
        _p("occupied_cooling_setpoint", unit=ParameterUnit.celsius, role=ParameterRole.control),
    ]
    warnings = audit_device_parameters(
        "Thermostat",
        params,
        ignore_similar_pairs=[("occupied_heating_setpoint", "occupied_cooling_setpoint")],
    )
    assert not any("near-duplicate" in w for w in warnings)


def test_flags_redundant_representation_group():
    params = [
        _p("current_hue", unit=ParameterUnit.percentage),
        _p("current_saturation", unit=ParameterUnit.percentage),
        _p("current_x", unit=ParameterUnit.percentage),
        _p("current_y", unit=ParameterUnit.percentage),
    ]
    assert any("redundant representations" in w for w in audit_device_parameters("Light", params))
