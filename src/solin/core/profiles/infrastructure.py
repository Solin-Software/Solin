"""Filesystem and QSettings adapters for profiles."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_ORG_NAME,
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
    QSETTINGS_PROFILE_SCOPED_APPS,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import (
    GlobalSettingsStore,
    ProfileAppSettingsStore,
    SettingsStore,
)
from solin.core.profiles.application import ProfileRegistryLoadError, ProfileService
from solin.core.profiles.models import ProfileInfo
from solin.core.profiles.settings import ProfileSettings
from solin.core.storage.json_repository import JsonFileRepository
from solin.core.storage.migration import merge_dirs

log = logging.getLogger(__name__)

_PROFILES_FILENAME = "profiles.json"
_BASE_APP_GLOBAL_KEYS = {
    SettingsKey.INSTALL_ID,
    SettingsKey.PENDING_PATCH_CLEANUP,
}


class JsonProfileRegistry:
    """Atomic JSON repository for the global profile registry."""

    def __init__(self, path: str | Path) -> None:
        self._json = JsonFileRepository(path)

    @property
    def path(self) -> Path:
        return self._json.path

    def load(self) -> list[ProfileInfo]:
        if not self._json.exists():
            return []
        try:
            data = self._json.read()
            if not isinstance(data, dict):
                raise ValueError("Profile registry root must be an object.")
            profiles = data.get("profiles", [])
            if not isinstance(profiles, list):
                raise ValueError("Profile registry 'profiles' must be a list.")
            return [ProfileInfo.from_dict(entry) for entry in profiles]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProfileRegistryLoadError(
                f"Could not load profile registry: {self.path}"
            ) from exc

    def save(self, profiles: Sequence[ProfileInfo]) -> None:
        self._json.write(
            {"profiles": [profile.to_dict() for profile in profiles]}
        )


class LocalProfileStorage:
    """Owns profile-scoped directories and legacy file migration."""

    def __init__(
        self,
        data_dir: str | Path,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._cache_dir = Path(cache_dir) if cache_dir else None

    def paths_for(self, profile_id: str) -> ProfilePaths:
        return ProfilePaths.from_roots(
            data_dir=self._data_dir,
            cache_dir=self._cache_dir,
            profile_id=profile_id,
        )

    def ensure_profile_dirs(self, profile_id: str) -> None:
        self.paths_for(profile_id).ensure_dirs()

    def delete_profile_data(self, profile_id: str) -> None:
        deletion = self.stage_profile_deletion(profile_id)
        deletion.commit()

    def stage_profile_deletion(
        self,
        profile_id: str,
    ) -> "LocalProfileDataDeletion":
        return LocalProfileDataDeletion.stage(self._profile_targets(profile_id))

    def copy_legacy_data(self, profile_id: str) -> None:
        paths = self.paths_for(profile_id)
        legacy_playlists = self._data_dir / "playlists.json"
        if legacy_playlists.is_file():
            paths.playlists_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_playlists, paths.playlists_file)

        for source, destination in (
            (self._data_dir / "images", paths.images_dir),
            (self._data_dir / "embedded", paths.embedded_dir),
        ):
            if source.is_dir():
                merge_dirs(source, destination)

    def cleanup_legacy_data(self) -> None:
        targets = (
            self._data_dir / "playlists.json",
            self._data_dir / "images",
            self._data_dir / "embedded",
        )
        for target in targets:
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except OSError as exc:
                log.warning("Failed to clean migrated legacy data %s: %s", target, exc)

    def _profile_targets(self, profile_id: str) -> tuple[Path, ...]:
        paths = self.paths_for(profile_id)
        targets = [
            paths.profile_dir,
            paths.native_webview_data_dir,
        ]
        if self._cache_dir is not None:
            targets.append(self._cache_dir / "profiles" / profile_id)
        if paths.native_webview_cache_dir is not None:
            targets.append(paths.native_webview_cache_dir)
        return tuple(dict.fromkeys(targets))


class LocalProfileDataDeletion:
    def __init__(self, staged: list[tuple[Path, Path]]) -> None:
        self._staged = staged

    @classmethod
    def stage(cls, targets: Sequence[Path]) -> "LocalProfileDataDeletion":
        staged: list[tuple[Path, Path]] = []
        try:
            for target in targets:
                if not target.exists():
                    continue
                quarantine = target.with_name(
                    f".{target.name}.delete-{uuid.uuid4().hex}"
                )
                target.rename(quarantine)
                staged.append((target, quarantine))
        except OSError:
            cls(staged).rollback()
            raise
        return cls(staged)

    def commit(self) -> None:
        for _, quarantine in self._staged:
            try:
                if quarantine.is_dir():
                    shutil.rmtree(quarantine)
                elif quarantine.exists():
                    quarantine.unlink()
            except OSError as exc:
                log.warning(
                    "Failed to remove quarantined profile data %s: %s",
                    quarantine,
                    exc,
                )
        self._staged.clear()

    def rollback(self) -> None:
        failures: list[tuple[Path, OSError]] = []
        for target, quarantine in reversed(self._staged):
            if not quarantine.exists():
                continue
            try:
                quarantine.rename(target)
            except OSError as exc:
                failures.append((target, exc))
        self._staged.clear()
        if failures:
            paths = ", ".join(str(path) for path, _ in failures)
            raise OSError(f"Could not restore staged profile data: {paths}")


class QSettingsProfilePreferences:
    """Profile selection, namespaces, cleanup, and legacy settings migration."""

    def __init__(
        self,
        global_settings: GlobalSettingsStore,
        profile_app_settings_for: Callable[[str], ProfileAppSettingsStore],
    ) -> None:
        self._global_settings = global_settings
        self._profile_app_settings_for = profile_app_settings_for

    def has_legacy_settings(self) -> bool:
        legacy = SettingsStore.for_namespace(
            QSETTINGS_ORG_NAME,
            QSETTINGS_PREFS_APP,
        )
        return bool(legacy.all_keys())

    def last_active_profile(self) -> str:
        return self._global_settings.last_active_profile()

    def set_last_active_profile(self, profile_id: str) -> None:
        self._global_settings.set_last_active_profile(profile_id)

    def clear_last_active_profile_if(
        self,
        profile_id: str,
        replacement: str,
    ) -> None:
        self._global_settings.clear_last_active_profile_if(
            profile_id,
            replacement,
        )

    def clear_profile_settings(self, profile_id: str) -> None:
        organization = f"{QSETTINGS_PROFILE_ORG_PREFIX}{profile_id}"
        stores = [
            (
                SettingsStore.for_namespace(organization, application),
                application,
            )
            for application in QSETTINGS_PROFILE_SCOPED_APPS
        ]
        snapshots = [
            (
                store,
                {
                    key: store.value(key)
                    for key in store.all_keys()
                },
            )
            for store, _ in stores
        ]
        try:
            for store, _ in stores:
                store.clear()
        except Exception:  # noqa: BLE001 - settings snapshot restoration boundary
            for store, values in snapshots:
                store.clear()
                for key, value in values.items():
                    store.set_value(key, value, sync=False)
                store.sync()
            raise

    def copy_legacy_settings(self, profile_id: str) -> None:
        destination_org = f"{QSETTINGS_PROFILE_ORG_PREFIX}{profile_id}"
        for application in (QSETTINGS_PREFS_APP, QSETTINGS_APP_APP):
            source = SettingsStore.for_namespace(
                QSETTINGS_ORG_NAME,
                application,
            )
            destination = SettingsStore.for_namespace(
                destination_org,
                application,
            )
            for key in source.all_keys():
                if (
                    application == QSETTINGS_APP_APP
                    and key in _BASE_APP_GLOBAL_KEYS
                ):
                    continue
                destination.set_value(key, source.value(key), sync=False)
            destination.sync()

    def cleanup_legacy_settings(self) -> None:
        for application in (QSETTINGS_PREFS_APP, QSETTINGS_APP_APP):
            source = SettingsStore.for_namespace(
                QSETTINGS_ORG_NAME,
                application,
            )
            try:
                if application == QSETTINGS_PREFS_APP:
                    source.clear()
                    continue
                for key in source.all_keys():
                    if key not in _BASE_APP_GLOBAL_KEYS:
                        source.remove(key, sync=False)
                source.sync()
            except Exception as exc:  # noqa: BLE001 - settings backend boundary
                log.warning(
                    "Failed to clean migrated legacy settings %s: %s",
                    application,
                    exc,
                )

    def prepare_profile_creation(self, profile_id: str) -> None:
        language = self._profile_app_settings_for(profile_id).app_language()
        if language:
            self._global_settings.set_bootstrap_language(language)


@dataclass(frozen=True, slots=True)
class ProfileRuntimeContext:
    paths: ProfilePaths
    settings: ProfileSettings


class ProfileRuntimeContextFactory:
    """Builds infrastructure context after a profile is selected."""

    def __init__(
        self,
        data_dir: str | Path,
        cache_dir: str | Path | None = None,
        *,
        profile_settings_for: Callable[[str], ProfileSettings],
    ) -> None:
        self._storage = LocalProfileStorage(data_dir, cache_dir)
        self._profile_settings_for = profile_settings_for

    def create(self, profile_id: str) -> ProfileRuntimeContext:
        return ProfileRuntimeContext(
            paths=self._storage.paths_for(profile_id),
            settings=self._profile_settings_for(profile_id),
        )


def create_local_profile_service(
    data_dir: str | Path,
    cache_dir: str | Path | None = None,
    *,
    global_settings: GlobalSettingsStore,
    profile_app_settings_for: Callable[[str], ProfileAppSettingsStore],
) -> ProfileService:
    data_root = Path(data_dir)
    return ProfileService(
        registry=JsonProfileRegistry(data_root / _PROFILES_FILENAME),
        storage=LocalProfileStorage(data_root, cache_dir),
        preferences=QSettingsProfilePreferences(
            global_settings,
            profile_app_settings_for,
        ),
    )
