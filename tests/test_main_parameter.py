from uuid import uuid4

from majordom_integration_sdk.schemas.parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterVisibility,
    next_main_parameter_value,
)


def _p(*, data_type, valid_values=None, default_value=None):
    return Parameter(
        id=uuid4(),
        name="p",
        data_type=data_type,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        valid_values=valid_values,
        default_value=default_value,
        integration_data=None,
    )


def test_cycle_toggles_between_two_values():
    assert next_main_parameter_value(0, [0, 4]) == 4
    assert next_main_parameter_value(4, [0, 4]) == 0


def test_cycle_rotates_and_wraps():
    assert next_main_parameter_value(1, [0, 1, 2]) == 2
    assert next_main_parameter_value(2, [0, 1, 2]) == 0


def test_single_value_cycle_is_a_button():
    assert next_main_parameter_value(99, [3]) == 3  # always sets to 3 regardless of current


def test_unknown_current_starts_at_first():
    assert next_main_parameter_value(None, [0, 4]) == 0
    assert next_main_parameter_value(7, [0, 4]) == 0


def test_empty_cycle_returns_none():
    assert next_main_parameter_value(0, []) is None
    assert next_main_parameter_value(0, None) is None


def test_enum_with_valid_values_can_be_main():
    assert _p(data_type=ParameterDataType.enum, valid_values={0: "off", 4: "on"}).can_be_main_parameter


def test_enum_without_valid_values_cannot_be_main():
    assert not _p(data_type=ParameterDataType.enum).can_be_main_parameter


def test_default_value_makes_any_type_main():
    assert _p(data_type=ParameterDataType.integer, default_value=b"\x00\x00\x00\x05").can_be_main_parameter


def test_valid_values_makes_any_type_main():
    assert _p(data_type=ParameterDataType.integer, valid_values={0: "off", 80: "bright"}).can_be_main_parameter


def test_main_cycle_from_valid_values():
    p = _p(data_type=ParameterDataType.integer, valid_values={80: "bright", 0: "off"})
    assert p.main_cycle == [0, 80]  # numeric order


def test_main_cycle_from_set_default_value():
    from majordom_integration_sdk.schemas.parameter import ParameterState

    p = _p(data_type=ParameterDataType.integer, valid_values={0: "off", 40: "dim", 80: "bright"})
    state = ParameterState.model_validate(p, from_attributes=True).with_default_value({0, 80})
    assert state.main_cycle == [0, 80]  # the curated subset wins over full valid_values
    assert state.default_value == b"[0, 80]"  # stored canonically as a JSON array, 1 value = 1-element array


def test_main_cycle_single_default_is_button():
    from majordom_integration_sdk.schemas.parameter import ParameterState

    p = _p(data_type=ParameterDataType.integer)
    state = ParameterState.model_validate(p, from_attributes=True).with_default_value(5)
    assert state.main_cycle == [5]


def test_main_cycle_bool_toggles():
    assert _p(data_type=ParameterDataType.bool).main_cycle == [False, True]


def test_main_cycle_none_command_is_not_a_cycle():
    assert _p(data_type=ParameterDataType.none).main_cycle is None


def test_decode_value_roundtrip():
    from majordom_integration_sdk.schemas.parameter import ParameterState

    p = _p(data_type=ParameterDataType.integer)
    state = ParameterState.model_validate(p, from_attributes=True).with_value(42)
    assert state.decode_value() == 42


def test_non_user_visibility_cannot_be_main():
    p = _p(data_type=ParameterDataType.bool)
    p.visibility = ParameterVisibility.setting
    assert not p.can_be_main_parameter
    p.visibility = ParameterVisibility.system
    assert not p.can_be_main_parameter
