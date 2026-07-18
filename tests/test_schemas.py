"""Schema behaviour that the SDK owns (and the Hub should re-home to)."""

from uuid import uuid4

from majordom_integration_sdk.schemas.parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterVisibility,
)


def _child(data_type: ParameterDataType) -> Parameter:
    return Parameter(
        id=uuid4(),
        name=f"child-{data_type}",
        data_type=data_type,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
    )


def test_encode_struct_length_prefixes_each_child():
    flag = _child(ParameterDataType.bool)
    count = _child(ParameterDataType.integer)
    struct_param = ParameterState(
        id=uuid4(),
        name="command-args",
        data_type=ParameterDataType.struct,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
        fields=[flag, count],
    )

    encoded = struct_param.encode_value({flag.id: True, count.id: 5})

    flag_bytes = ParameterState.model_validate(flag, from_attributes=True).encode_value(True)
    count_bytes = ParameterState.model_validate(count, from_attributes=True).encode_value(5)
    expected = len(flag_bytes).to_bytes(4, "big") + flag_bytes + len(count_bytes).to_bytes(4, "big") + count_bytes
    assert encoded == expected


def test_encode_struct_missing_child_is_empty():
    flag = _child(ParameterDataType.bool)
    struct_param = ParameterState(
        id=uuid4(),
        name="s",
        data_type=ParameterDataType.struct,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
        fields=[flag],
    )
    # No value supplied for the child -> zero-length segment.
    assert struct_param.encode_value({}) == (0).to_bytes(4, "big")


def test_encode_struct_without_schema_falls_back_to_json():
    # Matter maps arbitrary complex values (dicts and lists) to `struct` with no `fields`
    # schema; those must encode without crashing rather than assert a dict.
    import json

    p = ParameterState(
        id=uuid4(),
        name="raw",
        data_type=ParameterDataType.struct,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
    )
    assert p.encode_value([41]) == json.dumps([41]).encode()
    assert p.encode_value({"a": 1}) == json.dumps({"a": 1}).encode()
