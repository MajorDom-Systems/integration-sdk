"""What a controller can take in from a file or blob (a backup, a record, a document, a config or a shared template):
the Hub asks it what the blob offers, the user picks the parts, the controller applies only those."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self


class ImportMode(StrEnum):
    AMEND = "amend"  # merge into what the integration already has
    REPLACE = "replace"  # the picked parts overwrite what it has


@dataclass(frozen=True)
class ImportSource:
    """`id` is the blob's sha256: the Hub keeps an uploaded blob by it for a day, so `import_data` after
    `import_inspect` (or a retry) needs no second upload. `size` lets it refuse a blob before storing it."""

    id: str
    size: int
    data: bytes
    name: str | None = None
    media_type: str | None = None

    @classmethod
    def of(cls, data: bytes, name: str | None = None, media_type: str | None = None) -> Self:
        return cls(hashlib.sha256(data).hexdigest(), len(data), data, name, media_type)


@dataclass(frozen=True)
class ImportPart:
    kind: str  # each integration's own: "network", "device", "config", ...
    label: str
    id: str | None = None


@dataclass(frozen=True)
class ImportOffer:
    parts: Sequence[ImportPart]
    suggested_mode: ImportMode | None = None


@dataclass(frozen=True)
class ImportReport:
    applied: Sequence[ImportPart] = ()
    skipped: dict[ImportPart, str] = field(default_factory=dict)  # part -> why
