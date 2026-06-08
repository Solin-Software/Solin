"""Shared resolution of JW media language settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class JWMediaLanguageContext:
    """Effective JW media language plus the non-sign UI fallback."""

    api_code: str
    fallback_code: str
    is_sign_language: bool


def _clean_code(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _code_from(obj: object | None, name: str) -> str:
    return _clean_code(getattr(obj, name, "")) if obj is not None else ""


def _bool_from(obj: object | None, name: str) -> bool | None:
    if obj is None or not hasattr(obj, name):
        return None
    return bool(getattr(obj, name))


def jw_media_language_context(
    lang_manager: object | None,
    *,
    default_api_code: str = "E",
) -> JWMediaLanguageContext:
    """Resolve the JW media language and UI fallback from the app language state.

    The primary ``api_code`` follows the JW media language selected in Settings.
    The ``fallback_code`` is always the interface language and is therefore
    treated as non-sign by the media API fallback paths.
    """

    default_code = _clean_code(default_api_code) or "E"
    fallback_code = _code_from(lang_manager, "api_code") or default_code

    api_code = _code_from(lang_manager, "media_api_code")
    jw_lang_service = (
        getattr(lang_manager, "jw_lang_service", None)
        if lang_manager is not None else None
    )
    if not api_code:
        api_code = _code_from(jw_lang_service, "media_api_code")

    is_sign_language = _bool_from(lang_manager, "is_media_sign_language")
    if is_sign_language is None:
        is_sign_language = bool(_bool_from(jw_lang_service, "is_media_sign_language"))

    return JWMediaLanguageContext(
        api_code=api_code or fallback_code,
        fallback_code=fallback_code,
        is_sign_language=is_sign_language,
    )
