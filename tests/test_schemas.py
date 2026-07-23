"""Schema behaviour that the SDK owns — pythonic parameter values, generic over the value type V."""

from uuid import uuid4

from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterVisibility,
)


def _state(data_type: ParameterDataType, **kw) -> ParameterState:
    return ParameterState(
        id=uuid4(),
        name="p",
        data_type=data_type,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
        **kw,
    )


def test_scalar_value_is_pythonic_and_roundtrips():
    for dt, v in [
        (ParameterDataType.integer, 5),
        (ParameterDataType.bool, True),
        (ParameterDataType.decimal, 1.5),
        (ParameterDataType.string, "hi"),
    ]:
        s = _state(dt, value=v)
        assert s.value == v
        assert ParameterState.model_validate_json(s.model_dump_json()).value == v


def test_struct_value_is_a_plain_dict():
    s = _state(ParameterDataType.struct, value={"a": 1, "b": [2, 3]})
    assert s.value == {"a": 1, "b": [2, 3]}
    assert ParameterState.model_validate_json(s.model_dump_json()).value == {"a": 1, "b": [2, 3]}


def test_value_valid_values_and_default_share_one_type():
    # An int parameter: int value, int valid_values keys, int default_value — all V=int.
    s = _state(ParameterDataType.enum, value=0, valid_values={0: "off", 4: "on"}, default_value={0, 4})
    assert (s.value, s.valid_values, s.default_value) == (0, {0: "off", 4: "on"}, {0, 4})

    rt = ParameterState.model_validate_json(s.model_dump_json())
    assert rt.default_value == {0, 4}  # a set survives the JSON-array round-trip
    assert rt.valid_values == {0: "off", 4: "on"}  # int keys survive


def test_single_default_value_stays_scalar():
    s = _state(ParameterDataType.integer, default_value=5)
    assert s.default_value == 5  # a lone value is the scalar branch of `set[V] | V`, not a set
