"""Dev/pairing-time sanity checks on a device's mapped parameters — protocol-neutral, so every
integration (and the audit tools) share one implementation. See the "Parameter Visibility & UX"
recipe in the docs. All checks are advisory: they return human-readable warnings, never raise.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher

from .schemas.parameter import Parameter, ParameterUnit, ParameterVisibility

# A device's `user` bucket is the everyday device screen; more than this is a strong
# over-exposure smell (usually uncurated read-only attrs leaking through).
MAX_USER_PARAMETERS = 8

# Name-similarity ratio (0..1) above which two `user` params look like near-duplicates.
NAME_SIMILARITY_THRESHOLD = 0.86


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def audit_device_parameters(
    device_name: str,
    parameters: Iterable[Parameter],
    *,
    max_user: int = MAX_USER_PARAMETERS,
    similarity_threshold: float = NAME_SIMILARITY_THRESHOLD,
    ignore_similar_pairs: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Return advisory warnings about a device's parameter mapping.

    Checks:
      * too many `user`-visible parameters (over-exposure),
      * near-duplicate names in the `user` bucket (a normalized string-similarity ratio —
        `difflib`, close to a Levenshtein signal without a dependency),
      * same-unit + same-role duplicates in the `user` bucket (catches redundant *representations*
        of one quantity even when the names differ, e.g. colour `hue/sat` and `x/y`).

    `ignore_similar_pairs` whitelists legitimately-similar name pairs (order-insensitive), e.g.
    `("occupied_heating_setpoint", "occupied_cooling_setpoint")`.
    """
    warnings: list[str] = []
    user = [p for p in parameters if p.visibility == ParameterVisibility.user]

    if len(user) > max_user:
        names = ", ".join(p.name for p in user[:12])
        warnings.append(
            f"{device_name}: {len(user)} user-visible parameters (> {max_user}) — likely "
            f"over-exposed; review visibility curation. [{names}{' …' if len(user) > 12 else ''}]"
        )

    # Near-duplicate names among user params (a Levenshtein-like signal).
    whitelist = {frozenset(pair) for pair in ignore_similar_pairs}
    for i in range(len(user)):
        for j in range(i + 1, len(user)):
            a, b = user[i], user[j]
            if frozenset((a.name, b.name)) in whitelist:
                continue
            ratio = _name_similarity(a.name, b.name)
            if ratio >= similarity_threshold:
                warnings.append(
                    f"{device_name}: user params {a.name!r} and {b.name!r} have near-duplicate "
                    f"names (similarity {ratio:.2f}) — is one redundant?"
                )

    # Redundant *representations* of one quantity — several user params sharing a specific
    # (non-plain) unit + role + type, even with different names (e.g. colour hue/sat AND x/y).
    # Grouped and thresholded so ordinary multi-sensor devices don't trip it.
    groups: dict[tuple, list[str]] = defaultdict(list)
    for p in user:
        if p.unit != ParameterUnit.plain:
            groups[(p.unit, p.role, p.data_type)].append(p.name)
    for (unit, role, _dt), group_names in groups.items():
        if len(group_names) >= 3:
            warnings.append(
                f"{device_name}: {len(group_names)} user params share {unit.value}/{role.value} "
                f"({', '.join(group_names)}) — possible redundant representations."
            )

    return warnings
