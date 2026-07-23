import base64
import json
import struct
from collections.abc import Sequence
from types import NoneType
from typing import Any, Self
from uuid import UUID

# from models import DeviceModelParameter
from pydantic import Field, field_serializer, field_validator

from .base import Base, StrEnum, UUIdentifable

# TODO: consider adding clusters / groups / endpoints to the device model to group parameters like in zigbee

# OLD (Merlin-alpha):
# class ParameterDataType(StrEnum):
#     integer = "integer"  # uint8
#     decimal = "decimal"  # uint8 to be casted to [0...1] decimal range
#     bool = "bool"        # one-bit integer
#     enum = "enum"        # uint8 with string_representation
#     string = "string"    # string
# class ParameterUnit(StrEnum):
#     plain = "plain"                # any
#     humidity = "humidity"          # decimal;
#     temperature_c = "temperature_c" # float32;
#     color_temperature = "color_temperature"  # Kelvin, decimal; 0.5 is white
#     rgb = "rgb"                    # hue wheel angle, decimal;
#     volume = "volume"              # decimal;
#     button = "button"              # None, just a button
#     timeinterval = "timeinterval"  # seconds, int32;


class ParameterDataType(StrEnum):
    none = "none"  # e.g. button
    # numeric
    bool = "bool"
    integer = "integer"
    decimal = "decimal"  # python float
    enum = "enum"  # integer with string_representation
    # data
    string = "string"
    struct = "struct"  # multi-field object for things like Metter command arguments or just complex Parameters; Value format: {<child Parameter id>:<value>}
    data = "data"  # freeform binary data, base64 encoded at high level, for documents and extensions' internal usage
    # homekit also has array and dict
    # can be extended if needed


class ParameterUnit(StrEnum):
    plain = "plain"  # raw data type
    percentage = "percentage"
    # time
    second = "second"
    hertz = "hertz"
    # kinematic
    kilogram = "kilogram"
    arcdegree = "arcdegree"
    meters = "meters"
    mps = "mps"  # meters per second, speed
    mps2 = "mps2"  # meters per second squared, acceleration
    m3h = "m3h"  # cubic meters per hour, volumetric flow
    rpm = "rpm"  # revolutions per minute
    newton = "newton"  # force
    joule = "joule"  # energy
    kwh = "kwh"  # kilowatt-hour, energy (metering display unit)
    watt = "watt"  # power
    # temperature
    celsius = "celsius"
    kelvin = "kelvin"
    mired = "mired"  # reciprocal megakelvin, color temperature
    # electricity
    volt = "volt"
    ampere = "ampere"
    # light
    # check if lumen or candela are needed
    lux = "lux"
    # rgb = "rgb" # hex str; UPD: homekit implements color as separate simple HSV parameters w/o adding complex data structs
    # air
    pascal = "pascal"
    ppm = "ppm"  # parts per million, air quality
    ugm3 = "ugm3"  # micrograms per cubic meter, particulate matter (PM2.5/PM10)
    # informatics
    bytes = "bytes"  # data size
    bps = "bps"  # bytes per second, data rate
    json = "json"  # freeform json with code snippet display
    document = "document"  # upload/download files


class ParameterRole(StrEnum):
    sensor = "sensor"  # get-only
    control = "control"  # get-set
    event = "event"


class ParameterVisibility(StrEnum):
    user = "user"  # main, everyday interaction, device screen widgets (on/off, brightness, volume)
    setting = "setting"  # user-configurable but behind am extra "settings"/"advanced" tap: configured once and rarely touched again; or diagnostic readings (RSSI, firmware version)
    system = "system"  # hidden under-the-hood wirings; not visible to the user


def next_main_parameter_value(
    current: int | float | str | bool | None,
    cycle: Sequence[int | float | str] | None,
) -> int | float | str | None:
    """The value a one-tap (cycle/toggle) main parameter should send next.

    ``cycle`` is the ordered set of values to rotate through — a ``default_value`` in
    ``valid_values`` format (works for any data type, e.g. ``[0, preferred_level]`` as a toggle),
    or the parameter's full ``valid_values`` keys when no subset is set. A single-element cycle
    is a "set to this value" button. Returns the element after ``current`` (wrapping), or the
    first element when ``current`` isn't in the set (or is unknown). ``None`` for an empty cycle.
    """
    if not cycle:
        return None
    if len(cycle) == 1 or current not in cycle:
        return cycle[0]
    return cycle[(cycle.index(current) + 1) % len(cycle)]


def _sorted_values(values) -> list:
    """Cycle values in a deterministic order — numeric where the values are numbers. (Accepts a
    ``valid_values`` dict, iterating its keys, or any iterable of values, e.g. a decoded set.)"""
    try:
        return sorted(values, key=float)
    except (TypeError, ValueError):
        return list(values)


def _decode_default(parameter: "Parameter") -> list | None:
    """Decode ``default_value`` bytes to the canonical stored form: a JSON array of values
    (a single default is a 1-element array). Legacy scalar-encoded bytes decode to ``[value]``."""
    raw = parameter.default_value
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        loaded = None
    if isinstance(loaded, list):
        return loaded
    # Legacy/simple scalar encoding (pre-set-format writers).
    return [ParameterState.model_validate(parameter, from_attributes=True).decode_value(raw)]


class Parameter(UUIdentifable):
    id: UUID
    name: str
    # description: str TODO: consider this one for display/tooltips
    data_type: ParameterDataType
    unit: ParameterUnit = ParameterUnit.plain  # TODO: consider making str in case of an unsupported value (e.g. version mismatch). Alternativelely, consider adding a case like unknown<foobar>
    role: ParameterRole
    visibility: ParameterVisibility  # mainly for UX

    # value constraints (value for nubmers, char length for string, byte length for data)
    min_value: int | float | None = None
    max_value: int | float | None = None
    min_step: int | float | None = None

    # mainly for enums
    valid_values: dict[int | float | str, str] | None = None  # value and string representation
    fields: list["Parameter"] | None = None  # schema for data_type=struct
    # Integrations that expose commands with arguments (e.g. Matter) model the command itself as a
    # Parameter and each of its arguments as a nested Parameter in `fields` — command=parameter, arg=sub-parameter.

    default_value: bytes | None = None

    integration_data: Any

    @property
    def can_be_main_parameter(self) -> bool:
        """Whether this parameter can be a device's one-tap ``main_parameter`` (the room-tile
        shortcut). Requires ``user`` visibility — the main action is the most exposed control of
        all, so a settings/system parameter is never a candidate. Beyond that, eligible when a
        tap can do something meaningful:

        - ``bool`` — a toggle (each tap flips it); ``none`` — a button (fires the command);
        - ``valid_values`` set (any data type, not just enum) — a **cycle**: each tap advances
          to the next value (see :func:`next_main_parameter_value`);
        - ``default_value`` set — one value makes a "set to this value" button; a set of
          values makes a **cycle** for ANY data type (most commonly a two-value toggle, e.g.
          0 and a preferred level, but it can be longer).
        """
        return self.visibility == ParameterVisibility.user and bool(
            self.data_type in (ParameterDataType.bool, ParameterDataType.none)
            or self.default_value is not None
            or self.valid_values
        )

    @property
    def main_cycle(self) -> list[int | float | str | bool] | None:
        """The ordered values a main-parameter tap cycles through, or ``None`` when a tap isn't
        a cycle (a ``none`` command button, or nothing to derive from). Derivation, first match:

        - ``default_value`` -> its values, one value being a button (any data type);
        - ``valid_values`` -> its keys;
        - ``bool`` -> ``[False, True]`` (a toggle).

        Values are ordered numerically where possible, so e.g. off(0) -> on(4) -> wraps.
        """
        if self.default_value:
            decoded = _decode_default(self)
            if decoded:
                return _sorted_values(decoded)
        if self.valid_values:
            return _sorted_values(self.valid_values)
        if self.data_type == ParameterDataType.bool:
            return [False, True]
        return None

    # @classmethod
    # def from_orm(cls, obj):
    #     if isinstance(obj, DeviceModelParameter):
    #         return super().model_validate(obj.parameter)
    #     else:
    #         return super().model_validate(obj)


class ParameterVisibilityPatch(Base):
    visibility: ParameterVisibility


class ParameterState(Parameter):
    value: bytes = Field(default_factory=bytes)
    # device_id: UUID

    # model_config = {
    #     'json_encoders': {
    #         bytes: lambda v: base64.b64encode(v).decode()
    #     }
    # }

    @field_serializer("value")
    def serialize_value(self, v: bytes, _info) -> str:
        return base64.b64encode(v).decode()

    @field_validator("value", mode="before")
    @classmethod
    def parse_value(cls, v: Any) -> bytes:
        if v is None:
            return b""
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            return base64.b64decode(v)
        raise ValueError(
            f"Invalid value: value must be bytes or a base64 encoded string, got type '{type(v)}' with value '{v}'. To set a decoded value, use the 'with_value' method."
        )

    def with_value(self, v: Any) -> Self:
        self.value = self.encode_value(v)
        return self

    def with_default_value(self, v: Any) -> Self:
        """Set ``default_value`` from one value or a set/list of values (any data type).

        Stored canonically as a JSON array — a single value becomes a 1-element array (a tap
        "button"); several values become the tap cycle (see ``main_cycle``). Reading mirrors the
        stored form: ``_decode_default``/``main_cycle`` always yield the array. For ``data`` /
        ``struct`` parameters a single raw value is stored via ``encode_value`` as before.
        """
        if v is None:
            self.default_value = b""
            return self
        if self.data_type in (ParameterDataType.data, ParameterDataType.struct):
            self.default_value = self.encode_value(v)
            return self
        values = list(v) if isinstance(v, (set, frozenset, list, tuple)) else [v]
        for item in values:
            self.encode_value(item)  # type-check each against this parameter's data type
        self.default_value = json.dumps(_sorted_values(values)).encode()
        return self

    def decode_value(self, data: bytes | None = None) -> Any:
        """Inverse of :meth:`encode_value` for scalar types — decodes ``data`` (default: this
        state's own ``value``) back to a python value. Empty bytes decode to ``None``."""
        raw = self.value if data is None else data
        if not raw:
            return None
        match self.data_type:
            case ParameterDataType.integer | ParameterDataType.enum:
                return int.from_bytes(raw, "big", signed=True)
            case ParameterDataType.bool:
                return bool(raw[0])
            case ParameterDataType.decimal:
                return struct.unpack("d", raw)[0]
            case ParameterDataType.string:
                return raw.decode()
            case ParameterDataType.struct:
                try:
                    return json.loads(raw)
                except ValueError:
                    return raw
            case _:
                return raw

    def encode_value(self, v: Any) -> bytes:  # TODO: review this method and the opposite one
        def assert_type(expected_type):
            assert isinstance(v, expected_type), (
                f"Invalid value: value for {self.data_type} must be of type `{expected_type}`; got type {type(v)} with value {v}"
            )

        if v is None:
            return b""

        match self.data_type:
            case ParameterDataType.none:
                assert_type(NoneType)
                return b""

            case ParameterDataType.integer | ParameterDataType.enum:
                assert_type(int)
                # Width sized to the value (min 4 bytes for back-compat with fixed int32 readers).
                # Real Matter attributes carry uint32 values >= 2**31 (feature maps, bitmaps, event
                # numbers, epoch-us timestamps) that overflow a signed int32 — from_bytes decodes any width.
                length = max(4, (v.bit_length() // 8) + 1)
                return v.to_bytes(length, "big", signed=True)

            case ParameterDataType.bool:
                assert_type(bool)
                return bytes([int(v)])

            case ParameterDataType.decimal:
                assert_type(float)
                return struct.pack("d", v)

            case ParameterDataType.string:
                assert_type(str)
                return v.encode()

            case ParameterDataType.struct:
                # A schema'd struct ({child parameter id: child value} with a `fields` list)
                # is encoded field-by-field: each child against its own schema (recursively for
                # nested structs), length-prefixed (uint32 big-endian) so the buffer splits back
                # apart against the same `fields`, in order. Missing children encode as empty.
                if self.fields is not None and isinstance(v, dict):
                    buffer = bytearray()
                    for field in self.fields:
                        child = ParameterState.model_validate(field, from_attributes=True)
                        child_bytes = child.encode_value(v.get(field.id))
                        buffer += len(child_bytes).to_bytes(4, "big") + child_bytes
                    return bytes(buffer)
                # Otherwise `struct` is a catch-all for complex values without a declared field
                # schema — integrations (e.g. Matter) use it for arbitrary dicts/lists straight
                # off the protocol. Store those as JSON so the value round-trips without a schema.
                return json.dumps(v, default=str).encode()

            case ParameterDataType.data:
                return bytes(v)

            case ParameterDataType.struct:
                ...
                # TODO
        raise ValueError(f"Parameter of type {self.data_type} is not supported")

    # @property
    # def decoded_value(self) -> int | float | bool | str | bytes: # TODO: review deprecated, move to merlin
    #     match self.data_type:

    #         case ParameterDataType.integer | ParameterDataType.enum:
    #             return max(0, min(int.from_bytes(self.value, 'big'), 255)) # int as uint8 in [0, 255]

    #         case ParameterDataType.decimal:
    #             return max(0, min(int.from_bytes(self.value, 'big'), 255)) / 255 # float in [0, 1] mapped from uint8 in [0, 255]

    #         case ParameterDataType.bool:
    #             return bool(self.value[0])

    #         case ParameterDataType.string:
    #             return self.value.decode('utf-8') # utf-8 is default but explicit is better than implicit

    #         case _:
    #             return self.value
