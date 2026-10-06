import tempfile
from types import SimpleNamespace

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_ORG_NAME,
)
from solin.core.foundation import settings_store
from solin.core.foundation import identity
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.releases.channel import UpdateChannel


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

    assert store.last_active_profile() == "main_hall"
    assert store.bootstrap_language() == "pt_BR"
    assert _FakeSettings.buckets[(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)] == {
        SettingsKey.LAST_ACTIVE_PROFILE: "main_hall",
        SettingsKey.BOOTSTRAP_LANGUAGE: "pt_BR",
    }


def test_installation_settings_store_preserves_global_app_namespace(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    store = settings_store.InstallationSettingsStore.create()

    store.set_install_id("a" * 32)
    store.set_pending_update_cleanup_path("C:/Temp/Solin-update.exe")

    assert store.install_id() == "a" * 32
    assert store.pending_update_cleanup_path() == "C:/Temp/Solin-update.exe"
    assert _FakeSettings.buckets[(QSETTINGS_ORG_NAME, QSETTINGS_APP_APP)] == {
        SettingsKey.INSTALL_ID: "a" * 32,
        SettingsKey.PENDING_UPDATE_CLEANUP: "C:/Temp/Solin-update.exe",
    }

    store.clear_pending_update_cleanup_path()

    assert store.pending_update_cleanup_path() == ""


def test_installation_update_channel_is_typed_and_invalid_values_fall_back(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    store = settings_store.InstallationSettingsStore.create()

    assert store.update_channel() is UpdateChannel.STABLE
    store.set_update_channel(UpdateChannel.BETA)
    assert store.update_channel() is UpdateChannel.BETA

    _FakeSettings.buckets[(QSETTINGS_ORG_NAME, QSETTINGS_APP_APP)][
        SettingsKey.UPDATE_CHANNEL
    ] = "preview"
    assert store.update_channel() is UpdateChannel.STABLE


def test_legacy_patch_cleanup_only_removes_recognized_temporary_file(
    monkeypatch, tmp_path
) -> None:
    _install_fake_settings(monkeypatch)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    legacy = tmp_path / "Solin_patch_previous.exe"
    legacy.write_bytes(b"old patch")
    store = settings_store.InstallationSettingsStore.create()
    store.settings.set_value(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP, str(legacy))

    store.clean_legacy_update_download()

    assert not legacy.exists()
    assert store.settings.string(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP) == ""


def test_legacy_patch_cleanup_forgets_untrusted_path_without_deleting_it(
    monkeypatch, tmp_path
) -> None:
    _install_fake_settings(monkeypatch)
    outside = tmp_path / "unrelated.exe"
    outside.write_bytes(b"keep")
    store = settings_store.InstallationSettingsStore.create()
    store.settings.set_value(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP, str(outside))

    store.clean_legacy_update_download()

    assert outside.read_bytes() == b"keep"
    assert store.settings.string(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP) == ""


def test_get_install_id_uses_installation_settings_store(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    generated = "b" * 32
    store = settings_store.InstallationSettingsStore.create()
    monkeypatch.setattr(identity.uuid, "uuid4", lambda: SimpleNamespace(hex=generated))

    assert identity.get_install_id(store) == generated
    assert identity.get_install_id(store) == generated
    assert _FakeSettings.buckets[(QSETTINGS_ORG_NAME, QSETTINGS_APP_APP)] == {
        SettingsKey.INSTALL_ID: generated,
    }


def test_profile_app_settings_store_reads_language(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    _FakeSettings.buckets[("SolinDev_main_hall", QSETTINGS_APP_APP)] = {
        SettingsKey.APP_LANGUAGE: "fr_FR"
    }

    store = settings_store.ProfileAppSettingsStore.for_organization("SolinDev_main_hall")

    assert store.app_language() == "fr_FR"


def test_profile_app_settings_store_persists_theme_id(monkeypatch) -> None:
    _install_fake_settings(monkeypatch)
    store = settings_store.ProfileAppSettingsStore.for_organization("SolinDev_main_hall")

    assert store.app_theme_id() == "dark"

    store.set_app_theme_id("light")

    assert store.app_theme_id() == "light"
    assert _FakeSettings.buckets[("SolinDev_main_hall", QSETTINGS_APP_APP)] == {
        SettingsKey.APP_THEME: "light",
    }


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
