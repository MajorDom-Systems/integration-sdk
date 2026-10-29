"""How a settings model marks its fields for the host."""

from enum import StrEnum


class SettingsExtraField(StrEnum):
    """The JSON schema keywords a settings field is marked with: ``Field(json_schema_extra={Extra.read_only: True})``
    with ``from majordom_integration_sdk import SettingsExtraField as Extra``."""

    read_only = "readOnly"  # the peripheral fills it in itself; never the user, never kept
    write_only = "writeOnly"  # the user sets it, nothing reads it back: a secret
