from __future__ import annotations

from types import SimpleNamespace

from app.core.jw.language_context import jw_media_language_context


def test_jw_media_language_context_keeps_ui_language_as_fallback() -> None:
    lang = SimpleNamespace(
        api_code="T",
        media_api_code="ASL",
        is_media_sign_language=True,
    )

    context = jw_media_language_context(lang)

    assert context.api_code == "ASL"
    assert context.fallback_code == "T"
    assert context.is_sign_language is True


def test_jw_media_language_context_reads_raw_jw_service_when_needed() -> None:
    lang = SimpleNamespace(
        api_code="S",
        jw_lang_service=SimpleNamespace(
            media_api_code="E",
            is_media_sign_language=False,
        ),
    )

    context = jw_media_language_context(lang)

    assert context.api_code == "E"
    assert context.fallback_code == "S"
    assert context.is_sign_language is False


def test_jw_media_language_context_defaults_to_interface_language() -> None:
    lang = SimpleNamespace(
        api_code="T",
        media_api_code="",
        is_media_sign_language=False,
    )

    context = jw_media_language_context(lang)

    assert context.api_code == "T"
    assert context.fallback_code == "T"
    assert context.is_sign_language is False
