"""Checking a document against its collection's schema.

Artifact data is free-form JSON, which is what lets an agent build almost anything — and also lets it
(or a page) write `lastTouched: "yesterday"` where a date belongs, so the page silently breaks later.
A schema is optional, per collection, and small: field types, required fields, allowed values, number
ranges, string length, and whether unlisted fields are allowed. Every write goes through `check`, so
the writer gets a plain 422 naming the problem instead of the page failing somewhere else.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .exceptions import InvalidRequest

_MAX_REPORTED = 5


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_date(v: Any) -> bool:
    if not isinstance(v, str) or len(v) != 10:
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _is_datetime(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


_TYPES = {
    "string": (lambda v: isinstance(v, str), "text"),
    "number": (_is_number, "a number"),
    "integer": (lambda v: isinstance(v, int) and not isinstance(v, bool), "a whole number"),
    "boolean": (lambda v: isinstance(v, bool), "true or false"),
    "date": (_is_date, "a date as YYYY-MM-DD"),
    "datetime": (_is_datetime, "a date and time as ISO 8601"),
    "array": (lambda v: isinstance(v, list), "a list"),
    "object": (lambda v: isinstance(v, dict), "an object"),
    "any": (lambda v: True, "anything"),
}


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def problems(schema: dict[str, Any] | None, data: dict[str, Any]) -> list[str]:
    """Everything wrong with `data` under `schema` (empty when it matches or there is no schema)."""
    if not schema:
        return []
    fields: dict[str, dict[str, Any]] = schema.get("fields", {})
    found: list[str] = []
    for name, spec in fields.items():
        if name not in data or data[name] is None:
            if spec.get("required"):
                found.append(f"{name!r} is required")
            continue
        value = data[name]
        ok, wanted = _TYPES[spec["type"]]
        if not ok(value):
            found.append(f"{name!r} must be {wanted}, got {_short(value)}")
            continue
        if spec.get("enum") is not None and value not in spec["enum"]:
            found.append(f"{name!r} must be one of {spec['enum']}, got {_short(value)}")
        if _is_number(value):
            if spec.get("min") is not None and value < spec["min"]:
                found.append(f"{name!r} must be at least {spec['min']:g}, got {value}")
            if spec.get("max") is not None and value > spec["max"]:
                found.append(f"{name!r} must be at most {spec['max']:g}, got {value}")
        if isinstance(value, str) and spec.get("max_length") is not None and len(value) > spec["max_length"]:
            found.append(f"{name!r} is longer than {spec['max_length']} characters")
    if schema.get("strict"):
        found.extend(f"{name!r} is not a field of this collection" for name in data if name not in fields)
    return found


def check(collection: str, doc_id: str, schema: dict[str, Any] | None, data: dict[str, Any]) -> None:
    """Raise InvalidRequest (HTTP 422) naming the problems, or return if `data` is fine."""
    found = problems(schema, data)
    if not found:
        return
    shown = "; ".join(found[:_MAX_REPORTED])
    more = f"; and {len(found) - _MAX_REPORTED} more" if len(found) > _MAX_REPORTED else ""
    raise InvalidRequest(f"{collection}/{doc_id} doesn't match its schema: {shown}{more}")
