from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import field_validator

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

    ``cycle`` is the ordered set of values to rotate through — the parameter's ``default_value``
    values (a set), or its ``valid_values`` keys when no ``default_value`` is set. A single-element
    cycle is a "set to this value" button. Returns the element after ``current`` (wrapping), or the
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


class Parameter[V](UUIdentifable):
    """A device parameter. Generic over its value type ``V`` — ``value``, the keys of
    ``valid_values``, and ``default_value`` all share ``V`` (an ``int`` parameter has ``int``
    labels and an ``int`` default, a ``str`` parameter has ``str`` throughout, etc.)."""

    id: UUID
    name: str
    # Manufacturer-provided, read-only description (for display / tooltips). Integrations set it
    # when the protocol exposes one; the Hub never edits it.
    description: str | None = None
    # User-editable free-text note, stored MajorDom-side only (never sent to the device).
    note: str | None = None
    data_type: ParameterDataType
    unit: ParameterUnit = ParameterUnit.plain  # TODO: consider making str in case of an unsupported value (e.g. version mismatch). Alternativelely, consider adding a case like unknown<foobar>
    role: ParameterRole
    visibility: ParameterVisibility  # mainly for UX

    # value constraints (value for nubmers, char length for string, byte length for data)
    min_value: int | float | None = None
    max_value: int | float | None = None
    min_step: int | float | None = None

    # Allowed values -> display labels (an enum's members, or labelled presets). Keys are the same
    # type V as `value`. Mostly for enums; numeric parameters usually use min/max/step instead.
    valid_values: dict[V, str] | None = None
    fields: list[Any] | None = None  # sub-parameters (Parameter instances) for data_type=struct
    # Integrations that expose commands with arguments (e.g. Matter) model the command itself as a
    # Parameter and each of its arguments as a nested Parameter in `fields` — command=parameter, arg=sub-parameter.

    # The main-parameter tap value(s): one value is a button, a set is a cycle (2 = toggle,
    # 3+ = cycle). Same value type V as `value`. See `main_cycle` / `can_be_main_parameter`.
    default_value: set[V] | V | None = None

    integration_data: Any

    @field_validator("valid_values", mode="before")
    @classmethod
    def _coerce_valid_values_keys(cls, v, info):
        """Coerce `valid_values` keys to the parameter's value type. JSON object keys are always
        strings, so a stored ``{0: "off"}`` (int enum) comes back as ``{"0": "off"}`` — without a
        concrete ``V`` (heterogeneous parameter lists are used unparametrized) Pydantic can't
        recover the type. Drive it off ``data_type`` instead, so keys match ``value``."""
        if not isinstance(v, dict):
            return v
        coerce = {
            ParameterDataType.integer: int,
            ParameterDataType.enum: int,
            ParameterDataType.decimal: float,
            ParameterDataType.bool: bool,
        }.get(info.data.get("data_type"))
        if coerce is None:
            return v
        out = {}
        for key, label in v.items():
            try:
                out[coerce(key)] = label
            except (TypeError, ValueError):
                out[key] = label
        return out

    @property
    def can_be_main_parameter(self) -> bool:
        """Whether this parameter can be a device's one-tap ``main_parameter`` (the room-tile
        shortcut). Requires ``user`` visibility — the main action is the most exposed control of
        all, so a settings/system parameter is never a candidate. Beyond that, eligible when a
        tap can do something meaningful:

        - ``bool`` — a toggle (each tap flips it); ``none`` — a button (fires the command);
        - ``valid_values`` set (an enum) — a **cycle** through its values;
        - ``default_value`` set — one value is a button, a set is a cycle (2 = toggle, 3+ = cycle)
          for ANY data type (see :func:`next_main_parameter_value`).
        """
        return self.visibility == ParameterVisibility.user and bool(
            self.data_type in (ParameterDataType.bool, ParameterDataType.none)
            or self.default_value is not None
            or self.valid_values
        )

    @property
    def main_cycle(self) -> list | None:
        """The ordered values a main-parameter tap cycles through, or ``None`` when a tap isn't
        a cycle (a ``none`` command button, or nothing to derive from). Derivation, first match:

        - ``default_value`` -> its values (a set is a cycle; a single value is a one-element button);
        - ``valid_values`` -> its keys;
        - ``bool`` -> ``[False, True]`` (a toggle).

        Values are ordered numerically where possible, so e.g. off(0) -> on(4) -> wraps.
        """
        if self.default_value is not None:
            if isinstance(self.default_value, set):
                return _sorted_values(self.default_value)
            return [self.default_value]
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


class ParameterState[V](Parameter[V]):
    """A parameter plus its current ``value`` (same type ``V`` as the parameter's labels/default).

    Values are pythonic and serialize natively (Pydantic handles JSON — a ``set`` default_value
    becomes an array, etc.). The Hub persists them as JSON; integrations set ``value`` directly.
    """

    value: V | None = None
    # device_id: UUID
