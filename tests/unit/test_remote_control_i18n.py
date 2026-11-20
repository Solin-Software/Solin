from __future__ import annotations

from solin.core.i18n.remote_control import (
    REMOTE_CONTROL_TRANSLATION_SOURCES,
    _valid_remote_translation,
    remote_all_message_sources,
    remote_media_message_sources,
    remote_timer_message_sources,
)


def test_remote_message_catalogs_are_fully_visible_to_lupdate() -> None:
    media_sources = remote_media_message_sources()
    timer_sources = remote_timer_message_sources()
    all_sources = remote_all_message_sources()

    assert media_sources
    assert timer_sources
    assert set(all_sources) == set(media_sources.values()) | set(timer_sources.values())
    assert set(all_sources) == set(REMOTE_CONTROL_TRANSLATION_SOURCES)


def test_remote_translation_validation_preserves_placeholder_multiplicity() -> None:
    assert _valid_remote_translation(
        "Could not add {title}: {error}",
        "{error}: não foi possível adicionar {title}",
    )
    assert not _valid_remote_translation("{count} items", "{count} de {count} itens")
    assert not _valid_remote_translation("{count} items", "um | alguns | muitos")
