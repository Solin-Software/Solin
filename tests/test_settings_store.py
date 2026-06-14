from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_ORG_NAME,
)
from solin.core.foundation import settings_store
from solin.core.foundation.settings_keys import SettingsKey


class _FakeSettings:
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    synced: list[tuple[str, str]] = []
    cleared: list[tuple[str, str]] = []

    def __init__(self, organization: str, application: str):
        self.organization = organization
        self.application = application
        self.bucket = self.buckets.setdefault((organization, application), {})

    def allKeys(self):
        return list(self.bucket)

    def value(self, key, default=None, _type=None):
        return self.bucket.get(key, default)

    def setValue(self, key, value) -> None:
        self.bucket[key] = value

    def remove(self, key) -> None:
        self.bucket.pop(key, None)

    def clear(self) -> None:
        self.bucket.clear()
        self.cleared.append((self.organization, self.application))

    def sync(self) -> None:
        self.synced.append((self.organization, self.application))


def _install_fake_settings(monkeypatch):
    _FakeSettings.buckets = {}
    _FakeSettings.synced = []
    _FakeSettings.cleared = []
    monkeypatch.setattr(settings_store, "QSettings", _FakeSettings)


def test_global_settings_store_reads_and_writes_named_contracts(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    store = settings_store.GlobalSettingsStore.create()

    store.set_last_active_profile("main_hall")
    store.set_bootstrap_language("pt_BR")
    store.set_pending_patch_cleanup_path("C:/Temp/Solin_patch.exe")

    assert store.last_active_profile() == "main_hall"
    assert store.bootstrap_language() == "pt_BR"
    assert store.pending_patch_cleanup_path() == "C:/Temp/Solin_patch.exe"
    assert _FakeSettings.buckets[(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)] == {
        SettingsKey.LAST_ACTIVE_PROFILE: "main_hall",
        SettingsKey.BOOTSTRAP_LANGUAGE: "pt_BR",
        SettingsKey.PENDING_PATCH_CLEANUP: "C:/Temp/Solin_patch.exe",
    }

    store.clear_pending_patch_cleanup_path()

    assert store.pending_patch_cleanup_path() == ""


def test_profile_app_settings_store_reads_language(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    _FakeSettings.buckets[("SolinDev_main_hall", QSETTINGS_APP_APP)] = {
        SettingsKey.APP_LANGUAGE: "fr_FR"
    }

    store = settings_store.ProfileAppSettingsStore.for_organization("SolinDev_main_hall")

    assert store.app_language() == "fr_FR"


def test_settings_store_clears_removes_and_lists_keys(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    store = settings_store.SettingsStore.for_namespace("Org", "App")

    store.set_value("one", 1, sync=False)
    store.set_value("two", 2)
    assert store.all_keys() == ["one", "two"]

    store.remove("one")
    assert store.all_keys() == ["two"]

    store.clear()
    assert store.all_keys() == []
    assert _FakeSettings.cleared == [("Org", "App")]
