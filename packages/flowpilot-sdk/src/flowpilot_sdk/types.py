"""Payload type registry.

Every message type that travels over a port is a dataclass registered under a stable id of the form
``namespace.name/version`` (for example ``audio.pcm/1``). An incompatible change gets a new version.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

Kind = Literal["stream", "value"]

TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+/[1-9][0-9]*$")

_REGISTRY: dict[str, type[Any]] = {}

# Payload types that grant rights. Only nodes holding the mapped grant may produce them.
PRIVILEGED_TYPES: dict[str, str] = {}

# How each type travels: a stream of items, or one value. Ports of one type declare their kind;
# a port that takes several types (a union) gets the kind of the type connected to it.
_KINDS: dict[str, Kind] = {}


def validate_type_id(type_id: str) -> str:
    """Return ``type_id`` if it is well-formed, else raise ``ValueError``."""
    if not TYPE_ID_RE.match(type_id):
        raise ValueError(
            f"invalid payload type id {type_id!r}, expected 'namespace.name/<version>'"
        )
    return type_id


def payload_type[C: type[Any]](
    type_id: str, *, privileged_grant: str | None = None, kind: Kind = "stream"
) -> Callable[[C], C]:
    """Class decorator: register a payload dataclass under ``type_id`` (``kind``: how it
    travels when a port that takes several types carries it)."""
    validate_type_id(type_id)

    def register(cls: C) -> C:
        existing = _REGISTRY.get(type_id)
        if existing is not None and existing is not cls:
            raise ValueError(f"payload type id {type_id!r} is already registered by {existing!r}")
        _REGISTRY[type_id] = cls
        _KINDS[type_id] = kind
        cls.TYPE_ID = type_id
        if privileged_grant is not None:
            PRIVILEGED_TYPES[type_id] = privileged_grant
        return cls

    return register


def type_id_of(cls: type[Any]) -> str:
    """Return the registered type id of a payload class."""
    type_id = getattr(cls, "TYPE_ID", None)
    if not isinstance(type_id, str) or _REGISTRY.get(type_id) is not cls:
        raise LookupError(f"{cls!r} is not a registered payload type")
    return type_id


def payload_class(type_id: str) -> type[Any]:
    """Return the payload class registered under ``type_id``."""
    try:
        return _REGISTRY[type_id]
    except KeyError:
        raise LookupError(f"unknown payload type id {type_id!r}") from None


def registered_types() -> dict[str, type[Any]]:
    """Return a snapshot of all registered payload types."""
    return dict(_REGISTRY)


def type_kind(type_id: str) -> Kind:
    """How a registered type travels on a port that takes several types."""
    try:
        return _KINDS[type_id]
    except KeyError:
        raise LookupError(f"unknown payload type id {type_id!r}") from None


def union(*type_ids: str) -> str:
    """A port type that takes any of ``type_ids``: ``"a/1|b/1"``."""
    for type_id in type_ids:
        validate_type_id(type_id)
    if len(set(type_ids)) < 2:
        raise ValueError("a union needs at least two different types")
    return "|".join(type_ids)


# A port type for inputs that take every payload type, e.g. a debug tap. Like a union port, it
# has the kind of the type connected. Outputs cannot have it: they say what they give.
ANY = "*"


def union_members(port_type: str) -> tuple[str, ...]:
    """The types a port type stands for: one, the members of a union, or every registered type
    for ``ANY``."""
    if port_type == ANY:
        return tuple(_REGISTRY)
    return tuple(port_type.split("|"))
