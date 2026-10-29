"""What the SDK and integrations keep on disk themselves: JSON, written whole or not at all, for its owner alone.
The runtime keeps its state here, Compose its containers; an integration its own files (``documents_folder``)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_json(path: Path) -> dict[str, Any]:
    """What ``path`` holds; nothing while it is missing, or unreadable (it starts afresh)."""
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        log.error(f"{path} is unreadable, starting afresh: {error}")
        return {}
    if not isinstance(value, dict):
        log.error(f"{path} holds no object, starting afresh")
        return {}
    return value


def write_json(path: Path, value: Any):
    """Write ``value`` whole or not at all: a crash midway leaves the file as it was. Only its owner reads it: the
    settings and the rendered containers hold secrets (Z-Wave's network keys), an integration's own files too (the keys
    ESPHome set on its nodes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = path.with_name(f"{path.name}.tmp")
    written.touch(mode=0o600)
    written.chmod(0o600)  # one a crash left keeps its mode
    written.write_text(json.dumps(value, indent=2))
    os.replace(written, path)
