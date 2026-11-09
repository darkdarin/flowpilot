"""Settings made inputs of a node (``DYNAMIC_KEY``): what comes on them overrides the setting for
the turn, turned into the setting's type and checked against its schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

TRUE = {"true", "yes", "on", "1", "да", "вкл"}
FALSE = {"false", "no", "off", "0", "нет", "выкл"}


def coerce(value: Any, schema: Mapping[str, Any]) -> Any:
    """``value`` in the type the setting wants, when it can be read so ("0.8" → 0.8, "да" →
    True, 2 → "2" for text); as it is otherwise (the schema check then says no)."""
    if "enum" in schema:
        for allowed in schema["enum"]:
            if allowed == value or str(allowed) == str(value):
                return allowed
        return value
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds or [])
    if isinstance(value, bool):
        return value if "boolean" in kinds or not kinds else str(value).lower()
    text = str(value).strip()
    for kind in kinds:
        if kind == "boolean" and text.casefold() in TRUE | FALSE:
            return text.casefold() in TRUE
        if kind == "integer":
            try:
                number = float(text.replace(",", "."))
            except ValueError:
                continue
            if number.is_integer():
                return int(number)
        if kind == "number":
            try:
                return float(text.replace(",", "."))
            except ValueError:
                continue
        if kind == "string":
            return text
    return value


def fits(value: Any, schema: Mapping[str, Any]) -> str | None:
    """Why ``value`` does not fit the setting's schema; ``None`` when it does."""
    error = next(iter(Draft202012Validator(dict(schema)).iter_errors(value)), None)
    return error.message if error is not None else None
