from __future__ import annotations

from solin.core.i18n.remote_control import (
    REMOTE_CONTROL_TRANSLATION_SOURCES,
    _valid_remote_translation,
    remote_control_message_sources,
)


def test_remote_message_catalog_is_fully_visible_to_lupdate() -> None:
    sources = remote_control_message_sources()

    assert sources
    assert set(sources.values()) == set(REMOTE_CONTROL_TRANSLATION_SOURCES)


def test_remote_translation_validation_preserves_placeholder_multiplicity() -> None:
    assert _valid_remote_translation(
        "Could not add {title}: {error}",
        "{error}: não foi possível adicionar {title}",
    )
    assert not _valid_remote_translation("{count} items", "{count} de {count} itens")
    assert not _valid_remote_translation("{count} items", "um | alguns | muitos")
