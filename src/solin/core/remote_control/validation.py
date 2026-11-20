"""Shared JSON type aliases and primitive validators for remote-control contracts."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Final, TypeAlias
import uuid


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]

MAX_IDENTIFIER_LENGTH: Final = 512
MAX_DISPLAY_TEXT_LENGTH: Final = 4_096
MAX_ERROR_TEXT_LENGTH: Final = 1_024


def require_string(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
    maximum: int = MAX_DISPLAY_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    return value


def require_identifier(value: str, *, field_name: str) -> str:
    return require_string(
        value,
        field_name=field_name,
        maximum=MAX_IDENTIFIER_LENGTH,
    )


def require_uuid(value: str, *, field_name: str) -> str:
    require_identifier(value, field_name=field_name)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"{field_name} must use the canonical UUID representation")
    return value


def require_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def require_nonnegative_int(value: int, *, field_name: str) -> int:
    require_int(value, field_name=field_name)
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return value


def require_optional_nonnegative_int(value: int | None, *, field_name: str) -> None:
    if value is not None:
        require_nonnegative_int(value, field_name=field_name)


def require_finite_float(value: int | float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def payload_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def payload_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def payload_bool(payload: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def reject_unknown_fields(payload: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ValueError(f"unknown command field: {unknown[0]}")
