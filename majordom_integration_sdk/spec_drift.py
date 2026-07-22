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

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DriftReport:
    added: dict[Hashable, Any]
    removed: dict[Hashable, Any]
    reclassified: dict[Hashable, tuple[Any, Any]]  # key -> (old, new)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.reclassified)

    @property
    def has_high_risk(self) -> bool:
        """RECLASSIFY changes an existing item's judgment — the only tier that can silently alter
        what current users already see, so it always warrants human review."""
        return bool(self.reclassified)

    def render(self, *, source: str = "spec") -> str:
        if self.is_empty:
            return f"[{source}] no drift"
        lines = [
            f"[{source}] drift: +{len(self.added)} added  -{len(self.removed)} removed  "
            f"~{len(self.reclassified)} reclassified"
        ]
        # RECLASSIFY first — it's the high-risk tier.
        for key, (old, new) in sorted(self.reclassified.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  ~ RECLASSIFY {key!r}: {old!r} -> {new!r}")
        for key, val in sorted(self.added.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  + ADD        {key!r}: {val!r}")
        for key, val in sorted(self.removed.items(), key=lambda kv: repr(kv[0])):
            lines.append(f"  - REMOVE     {key!r}: {val!r}")
        return "\n".join(lines)


def diff_specs(current: Mapping[Hashable, Any], baseline: Mapping[Hashable, Any]) -> DriftReport:
    """Diff a freshly-harvested ``current`` spec against the committed ``baseline``."""
    added = {k: current[k] for k in current if k not in baseline}
    removed = {k: baseline[k] for k in baseline if k not in current}
    reclassified = {
        k: (baseline[k], current[k]) for k in current if k in baseline and current[k] != baseline[k]
    }
    return DriftReport(added=added, removed=removed, reclassified=reclassified)
