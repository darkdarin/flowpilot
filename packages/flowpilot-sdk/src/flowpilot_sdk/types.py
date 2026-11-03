"""Payload type registry.

Every message type that travels over a port is a dataclass registered under a stable id of the form
``namespace.name/version`` (for example ``audio.pcm/1``). An incompatible change gets a new version.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+/[1-9][0-9]*$")

_REGISTRY: dict[str, type[Any]] = {}

# Payload types that grant rights. Only nodes holding the mapped grant may produce them.
PRIVILEGED_TYPES: dict[str, str] = {}


def validate_type_id(type_id: str) -> str:
    """Return ``type_id`` if it is well-formed, else raise ``ValueError``."""
    if not TYPE_ID_RE.match(type_id):
        raise ValueError(
            f"invalid payload type id {type_id!r}, expected 'namespace.name/<version>'"
        )
    return type_id


def payload_type[C: type[Any]](
    type_id: str, *, privileged_grant: str | None = None
) -> Callable[[C], C]:
    """Class decorator: register a payload dataclass under ``type_id``."""
    validate_type_id(type_id)

    def register(cls: C) -> C:
        existing = _REGISTRY.get(type_id)
        if existing is not None and existing is not cls:
            raise ValueError(f"payload type id {type_id!r} is already registered by {existing!r}")
        _REGISTRY[type_id] = cls
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
