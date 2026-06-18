from __future__ import annotations

import uuid

from PySide6.QtCore import QCoreApplication

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.jw.languages import JWLanguageService
from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.profiles.settings import ProfileSettings


def _store() -> JWLanguageSettingsStore:
    settings = ProfileSettings.for_profile_id(f"jw_language_{uuid.uuid4().hex}")
    return JWLanguageSettingsStore.for_profile_settings(settings)


def _application() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def test_jw_language_settings_store_roundtrips_media_language() -> None:
    store = _store()
    store.settings.clear()
    store.legacy_settings.clear()
    try:
        store.set_media_language_code("T")

        assert store.media_language_code() == "T"
    finally:
        store.settings.clear()
        store.legacy_settings.clear()


def test_jw_language_settings_store_migrates_legacy_media_language() -> None:
    store = _store()
    store.settings.clear()
    store.legacy_settings.clear()
    try:
        store.legacy_settings.set_value(SettingsKey.LEGACY_JW_LANGUAGE, "S")

        assert store.media_language_code() == "S"
        assert store.settings.string(SettingsKey.MEDIA_LANGUAGE_CODE) == "S"
    finally:
        store.settings.clear()
        store.legacy_settings.clear()


def test_jw_language_service_uses_activated_settings_store(tmp_path) -> None:
    _application()
    store = _store()
    store.settings.clear()
    store.legacy_settings.clear()
    try:
        store.set_media_language_code("T")
        service = JWLanguageService(cache_file=tmp_path / "jw_languages.json")
        service.activate_settings(store)

        assert service.media_api_code == "T"

        service.set_media_api_code("E")

        assert store.media_language_code() == "E"
    finally:
        store.settings.clear()
        store.legacy_settings.clear()
