import uuid

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from solin.core.profiles.settings import ProfileSettings


def _store() -> tuple[WatchedFolderSettingsStore, SettingsStore]:
    profile_settings = ProfileSettings.for_profile_id(
        f"watched_folder_{uuid.uuid4().hex}"
    )
    settings = SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )
    return WatchedFolderSettingsStore(settings), settings


def test_watched_folder_settings_roundtrips_and_clears_path():
    store, settings = _store()
    settings.clear()
    try:
        assert store.path() == ""

        store.set_path("C:/Meetings")

        assert store.path() == "C:/Meetings"
        assert settings.string(SettingsKey.WATCHED_FOLDER_PATH) == "C:/Meetings"

        store.clear_path()

        assert store.path() == ""
        assert SettingsKey.WATCHED_FOLDER_PATH not in settings.all_keys()
    finally:
        settings.clear()
