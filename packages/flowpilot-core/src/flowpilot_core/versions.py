"""Minimal semver matching for node version ranges in graph.json.

Supported ranges: ``*``, exact ``X.Y.Z``, caret ``^X``, ``^X.Y``, ``^X.Y.Z`` and tilde ``~X.Y``,
``~X.Y.Z``. Caret follows npm semantics, including the special cases for ``0.x``.
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PARTIAL_RE = re.compile(r"^(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?$")

Version = tuple[int, int, int]


def parse_version(value: str) -> Version:
    m = _VERSION_RE.match(value)
    if not m:
        raise ValueError(f"invalid version {value!r}")
    return int(m[1]), int(m[2]), int(m[3])


def _bounds(range_: str) -> tuple[Version, Version | None]:
    """Return ``[lower, upper)`` for a range; ``upper`` is ``None`` when unbounded."""
    if range_ == "*":
        return (0, 0, 0), None
    op, rest = (range_[0], range_[1:]) if range_[:1] in ("^", "~") else ("", range_)
    m = _PARTIAL_RE.match(rest)
    if not m:
        raise ValueError(f"invalid version range {range_!r}")
    major = int(m[1])
    minor = int(m[2]) if m[2] is not None else None
    patch = int(m[3]) if m[3] is not None else None
    lower = (major, minor or 0, patch or 0)
    if op == "":
        if minor is None or patch is None:
            raise ValueError(f"exact version must be X.Y.Z, got {range_!r}")
        return lower, (major, minor, patch + 1)
    if op == "~":
        if minor is None:
            raise ValueError(f"tilde range needs at least X.Y, got {range_!r}")
        return lower, (major, minor + 1, 0)
    # caret
    if major > 0 or minor is None:
        return lower, (major + 1, 0, 0)
    if minor > 0 or patch is None:
        return lower, (0, minor + 1, 0)
    return lower, (0, 0, patch + 1)


def validate_range(range_: str) -> None:
    _bounds(range_)


def satisfies(version: str, range_: str) -> bool:
    v = parse_version(version)
    lower, upper = _bounds(range_)
    return v >= lower and (upper is None or v < upper)
