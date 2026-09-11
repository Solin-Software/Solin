from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSettings

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_ORG_NAME,
)
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.releases.channel import UpdateChannel


@dataclass(frozen=True, slots=True)
class SettingsNamespace:
    organization: str
    application: str


@dataclass(frozen=True, slots=True)
class SettingsStore:
    namespace: SettingsNamespace

    @classmethod
    def for_namespace(cls, organization: str, application: str) -> SettingsStore:
        return cls(SettingsNamespace(organization, application))

    def _settings(self) -> QSettings:
        return QSettings(self.namespace.organization, self.namespace.application)

    def all_keys(self) -> list[str]:
        return list(self._settings().allKeys())

    def value(
        self,
        key: str,
        default: Any = None,
        value_type: type | None = None,
    ) -> Any:
        settings = self._settings()
        if value_type is None:
            return settings.value(key, default)
        return settings.value(key, default, value_type)

    def string(self, key: str, default: str = "") -> str:
        return str(self.value(key, default, str) or "")

    def set_value(self, key: str, value: Any, *, sync: bool = True) -> None:
        settings = self._settings()
        settings.setValue(key, value)
        if sync:
            settings.sync()

    def remove(self, key: str, *, sync: bool = True) -> None:
        settings = self._settings()
        settings.remove(key)
        if sync:
            settings.sync()

    def clear(self, *, sync: bool = True) -> None:
        settings = self._settings()
        settings.clear()
        if sync:
            settings.sync()

    def sync(self) -> None:
        self._settings().sync()


@dataclass(frozen=True, slots=True)
class GlobalSettingsStore:
    settings: SettingsStore

    @classmethod
    def create(cls) -> GlobalSettingsStore:
        return cls(SettingsStore.for_namespace(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP))

    def last_active_profile(self) -> str:
        return self.settings.string(SettingsKey.LAST_ACTIVE_PROFILE)

    def set_last_active_profile(self, profile_id: str) -> None:
        self.settings.set_value(SettingsKey.LAST_ACTIVE_PROFILE, profile_id)

    def clear_last_active_profile_if(self, profile_id: str, replacement: str) -> None:
        if self.last_active_profile() == profile_id:
            self.set_last_active_profile(replacement)

    def bootstrap_language(self) -> str:
        return self.settings.string(SettingsKey.BOOTSTRAP_LANGUAGE)

    def set_bootstrap_language(self, language: str) -> None:
        self.settings.set_value(SettingsKey.BOOTSTRAP_LANGUAGE, language)


@dataclass(frozen=True, slots=True)
class InstallationSettingsStore:
    settings: SettingsStore

    @classmethod
    def create(cls) -> InstallationSettingsStore:
        return cls(SettingsStore.for_namespace(QSETTINGS_ORG_NAME, QSETTINGS_APP_APP))

    def install_id(self) -> str:
        return self.settings.string(SettingsKey.INSTALL_ID)

    def set_install_id(self, install_id: str) -> None:
        self.settings.set_value(SettingsKey.INSTALL_ID, install_id)

    def update_channel(self) -> UpdateChannel:
        try:
            return UpdateChannel(self.settings.string(SettingsKey.UPDATE_CHANNEL, "stable"))
        except ValueError:
            return UpdateChannel.STABLE

    def set_update_channel(self, channel: UpdateChannel) -> None:
        self.settings.set_value(SettingsKey.UPDATE_CHANNEL, UpdateChannel(channel).value)

    def pending_update_cleanup_path(self) -> str:
        return self.settings.string(SettingsKey.PENDING_UPDATE_CLEANUP)

    def set_pending_update_cleanup_path(self, path: str) -> None:
        self.settings.set_value(SettingsKey.PENDING_UPDATE_CLEANUP, path)

    def clear_pending_update_cleanup_path(self) -> None:
        self.settings.remove(SettingsKey.PENDING_UPDATE_CLEANUP)

    def clean_legacy_update_download(self) -> None:
        """Retire old patch state without following arbitrary persisted paths."""
        import tempfile
        from pathlib import Path

        value = self.settings.string(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP)
        if not value:
            return
        path = Path(value)
        valid = (
            not path.is_symlink()
            and path.name.startswith("Solin_patch_")
            and path.suffix == ".exe"
            and path.resolve().parent == Path(tempfile.gettempdir()).resolve()
        )
        if valid:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return  # Retain the old key until a subsequent startup can remove the file.
        self.settings.remove(SettingsKey.LEGACY_PENDING_PATCH_CLEANUP)


@dataclass(frozen=True, slots=True)
class ProfileAppSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_organization(cls, organization: str) -> ProfileAppSettingsStore:
        return cls(SettingsStore.for_namespace(organization, QSETTINGS_APP_APP))

    def app_language(self) -> str:
        return self.settings.string(SettingsKey.APP_LANGUAGE)

    def set_app_language(self, language: str) -> None:
        self.settings.set_value(SettingsKey.APP_LANGUAGE, language)

    def app_theme_id(self) -> str:
        return self.settings.string(SettingsKey.APP_THEME, "dark")

    def set_app_theme_id(self, theme_id: str) -> None:
        self.settings.set_value(SettingsKey.APP_THEME, str(theme_id or "").strip())

    def hover_popup_ids(self) -> frozenset[str]:
        value = self.settings.value(SettingsKey.TOOLBAR_HOVER_POPUPS, [])
        if not isinstance(value, (list, tuple)):
            return frozenset()
        return frozenset(item for item in value if isinstance(item, str) and item)

    def set_popup_hover_enabled(self, popup_id: str, enabled: bool) -> None:
        popup_ids = set(self.hover_popup_ids())
        if enabled:
            popup_ids.add(popup_id)
        else:
            popup_ids.discard(popup_id)
        self.settings.set_value(SettingsKey.TOOLBAR_HOVER_POPUPS, sorted(popup_ids))

    def scenes_panel_docked(self) -> bool:
        """Whether the scenes panel was left attached to the window bottom."""
        value = self.settings.value(SettingsKey.SCENES_PANEL_DOCKED, False)
        if isinstance(value, bool):
            return value
        # QSettings round-trips booleans as strings on some backends.
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def set_scenes_panel_docked(self, docked: bool) -> None:
        self.settings.set_value(SettingsKey.SCENES_PANEL_DOCKED, bool(docked))
