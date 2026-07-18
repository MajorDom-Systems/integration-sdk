from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel as PydanticBaseModel
from pydantic import GetJsonSchemaHandler
from pydantic_core import core_schema as cs
from pydantic_discriminator import DiscriminatedBaseModel

# try patching the pydantic-discriminator to fix "object does not support item assignment" at "v[Naming.TYPE_FIELD_ALIAS] = cls.discriminator()" for Scene Id
# TODO: fix it in pydantic-discriminator
original = DiscriminatedBaseModel._validate_type_field.__func__


def patched(cls, v):
    if not isinstance(v, dict):
        return v
    return original(cls, v)


DiscriminatedBaseModel._validate_type_field = classmethod(patched)
# end patch


class StrEnum(str, Enum):
    pass


class Base(PydanticBaseModel):  # TODO: make sure all models use it
    model_config = {
        "from_attributes": True,
        "validate_assignment": True,
    }


class TypedBaseModel(DiscriminatedBaseModel): ...


class NonEmptyStr(str):
    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler) -> cs.CoreSchema:
        # Create a core schema of type string with a validator function
        return cs.str_schema(
            min_length=1,
            strict=True,
            # You can add custom validation here if needed, but min_length=1 ensures non-empty
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: cs.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> dict:
        json_schema = handler(core_schema)
        json_schema.update(
            title="NonEmptyStr",
            description="A non-empty string",
            examples=["example text"],
        )
        return json_schema


class UUIdentifable(Base):  # TODO: review
    id: UUID

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id


class StrIdentifiable(Base):
    id: str
