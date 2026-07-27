"""Source-agnostic spec-drift diffing — the shared engine behind the MVD canary and the zha /
matter-HA harvest refreshers.

A "spec" here is any ``dict`` keyed by an identity (e.g. ``(cluster_id, attribute_id)``) whose
values are the harvested judgment. :func:`diff_specs` compares a freshly-produced spec against the
committed baseline and tiers every change by risk, mirroring how Dependabot tiers a version bump:

    ADD          a key that didn't exist          — low risk (reveals a previously-uncurated item)
    REMOVE       a key that disappeared            — medium (something we surfaced may vanish)
    RECLASSIFY   an existing key's value changed   — HIGH (changes what current users already see)

CI runs the harvester, calls :func:`diff_specs` against the vendored artifact, and opens a
Dependabot-style PR when anything changed — highlighting RECLASSIFY at the top for human review.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Any


# Generic over the key type — `Mapping` is invariant in its key, so a plain
# `Mapping[Hashable, Any]` would reject perfectly valid concrete specs like
# `dict[tuple[int, int], str]`.
@dataclass(frozen=True)
class DriftReport[K: Hashable]:
    added: dict[K, Any]
    removed: dict[K, Any]
    reclassified: dict[K, tuple[Any, Any]]  # key -> (old, new)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.reclassified)

    @property
    def has_high_risk(self) -> bool:
        """RECLASSIFY changes an existing item's judgment — the only tier that can silently alter
        what current users already see, so it always warrants human review."""
        return bool(self.reclassified)

    def render(self, *, source: str = "spec", key_label: Callable[[K], str] | None = None) -> str:
        """Render a human-readable drift summary.

        ``key_label`` optionally resolves a key to a human name (e.g. a raw ``(cluster_id,
        attribute_id)`` -> ``"Chime.SelectedChime"``) so a reviewer reading the PR body sees what
        actually changed, not just numeric ids. It's kept out of the engine because the mapping is
        integration-specific (chip bindings for matter, zigpy for zha); each canary passes its own.
        A resolver that raises or returns falsy for a key falls back to the bare id.
        """

        def tag(key: K) -> str:
            label = None
            if key_label is not None:
                try:
                    label = key_label(key)
                except Exception:  # noqa: BLE001 - a name lookup must never break the drift report
                    label = None
            return f"{label}  {key!r}" if label else f"{key!r}"

        if self.is_empty:
            return f"[{source}] no drift"
        lines = [
            f"[{source}] drift: +{len(self.added)} added  -{len(self.removed)} removed  "
            f"~{len(self.reclassified)} reclassified"
        ]
        # RECLASSIFY first — it's the high-risk tier.
        for key, (old, new) in sorted(self.reclassified.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  ~ RECLASSIFY {tag(key)}: {old!r} -> {new!r}")
        for key, val in sorted(self.added.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  + ADD        {tag(key)}: {val!r}")
        for key, val in sorted(self.removed.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  - REMOVE     {tag(key)}: {val!r}")
        return "\n".join(lines)


def diff_specs[K: Hashable](current: Mapping[K, Any], baseline: Mapping[K, Any]) -> DriftReport[K]:
    """Diff a freshly-harvested ``current`` spec against the committed ``baseline``."""
    added = {k: current[k] for k in current if k not in baseline}
    removed = {k: baseline[k] for k in baseline if k not in current}
    reclassified = {
        k: (baseline[k], current[k]) for k in current if k in baseline and current[k] != baseline[k]
    }
    return DriftReport(added=added, removed=removed, reclassified=reclassified)
