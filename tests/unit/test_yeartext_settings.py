import uuid

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.jw.yeartext_settings import YeartextSettingsStore
from solin.core.profiles.settings import ProfileSettings


def _store() -> tuple[YeartextSettingsStore, SettingsStore]:
    profile_settings = ProfileSettings.for_profile_id(f"yeartext_{uuid.uuid4().hex}")
    settings = SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )
    return YeartextSettingsStore(settings), settings


def test_yeartext_settings_roundtrips_quote_and_reference():
    store, settings = _store()
    settings.clear()
    try:
        assert store.text() == ("", "")

        store.set_text("Happy are those conscious of their spiritual need.", "Matthew 5:3")

        assert store.text() == (
            "Happy are those conscious of their spiritual need.",
            "Matthew 5:3",
        )
        assert (
            settings.string(SettingsKey.YEARLY_QUOTE)
            == "Happy are those conscious of their spiritual need."
        )
        assert settings.string(SettingsKey.YEARLY_REFERENCE) == "Matthew 5:3"

        store.set_text("", "")

        assert store.text() == ("", "")
    finally:
        settings.clear()
