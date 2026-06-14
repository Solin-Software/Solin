"""Framework-independent profile application service and ports."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from solin.core.profiles.models import (
    ProfileInfo,
    normalize_profile_name,
    profile_slug,
    unique_profile_slug,
)

log = logging.getLogger(__name__)


class ProfileRegistryLoadError(RuntimeError):
    """Raised when the persisted profile registry cannot be trusted."""


class ProfileRegistry(Protocol):
    def load(self) -> list[ProfileInfo]: ...

    def save(self, profiles: Sequence[ProfileInfo]) -> None: ...


class ProfileDataDeletion(Protocol):
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class ProfileStorage(Protocol):
    def ensure_profile_dirs(self, profile_id: str) -> None: ...

    def delete_profile_data(self, profile_id: str) -> None: ...

    def stage_profile_deletion(
        self,
        profile_id: str,
    ) -> ProfileDataDeletion: ...

    def copy_legacy_data(self, profile_id: str) -> None: ...

    def cleanup_legacy_data(self) -> None: ...


class ProfilePreferences(Protocol):
    def has_legacy_settings(self) -> bool: ...

    def last_active_profile(self) -> str: ...

    def set_last_active_profile(self, profile_id: str) -> None: ...

    def clear_last_active_profile_if(
        self,
        profile_id: str,
        replacement: str,
    ) -> None: ...

    def clear_profile_settings(self, profile_id: str) -> None: ...

    def copy_legacy_settings(self, profile_id: str) -> None: ...

    def cleanup_legacy_settings(self) -> None: ...

    def prepare_profile_creation(self, profile_id: str) -> None: ...


class ProfileService:
    """Coordinates profile lifecycle without Qt or persistence details."""

    def __init__(
        self,
        registry: ProfileRegistry,
        storage: ProfileStorage,
        preferences: ProfilePreferences,
    ) -> None:
        self._registry = registry
        self._storage = storage
        self._preferences = preferences
        self._active_id = ""
        self._profiles = registry.load()
        log.debug("Profile service initialized with %d profile(s)", len(self._profiles))

    @property
    def profiles(self) -> list[ProfileInfo]:
        return list(self._profiles)

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def active_profile(self) -> ProfileInfo | None:
        return self.get_profile(self._active_id)

    def get_profile(self, profile_id: str) -> ProfileInfo | None:
        return next(
            (profile for profile in self._profiles if profile.id == profile_id),
            None,
        )

    def has_profiles(self) -> bool:
        return bool(self._profiles)

    def has_legacy_settings(self) -> bool:
        return self._preferences.has_legacy_settings()

    def create_profile(self, name: str) -> ProfileInfo:
        normalized_name = normalize_profile_name(name)
        existing_ids = {profile.id for profile in self._profiles}
        profile_id = unique_profile_slug(
            profile_slug(normalized_name),
            existing_ids,
        )
        profile = ProfileInfo(id=profile_id, name=normalized_name)

        self._storage.ensure_profile_dirs(profile.id)
        updated = [*self._profiles, profile]
        try:
            self._registry.save(updated)
        except Exception:  # noqa: BLE001 - registry transaction boundary
            self._storage.delete_profile_data(profile.id)
            raise
        self._profiles = updated
        log.info("Profile created: %r (%s)", normalized_name, profile.id)
        return profile

    def rename_profile(self, profile_id: str, new_name: str) -> bool:
        current = self.get_profile(profile_id)
        if current is None:
            return False

        renamed = current.renamed(new_name)
        updated = [
            renamed if profile.id == profile_id else profile
            for profile in self._profiles
        ]
        self._registry.save(updated)
        self._profiles = updated
        log.info("Profile renamed: %s -> %r", profile_id, renamed.name)
        return True

    def delete_profile(self, profile_id: str) -> bool:
        if len(self._profiles) <= 1 or self.get_profile(profile_id) is None:
            return False

        was_active = profile_id == self._active_id
        deletion = self._storage.stage_profile_deletion(profile_id)
        previous = self._profiles
        updated = [
            profile for profile in self._profiles if profile.id != profile_id
        ]
        try:
            self._registry.save(updated)
        except Exception:  # noqa: BLE001 - staged deletion transaction boundary
            deletion.rollback()
            raise
        self._profiles = updated

        try:
            self._preferences.clear_profile_settings(profile_id)
        except Exception:  # noqa: BLE001 - settings deletion transaction boundary
            try:
                self._registry.save(previous)
                self._profiles = previous
            finally:
                deletion.rollback()
            raise
        deletion.commit()

        replacement_id = updated[0].id
        if was_active:
            self.set_active(replacement_id)
        else:
            self._preferences.clear_last_active_profile_if(
                profile_id,
                replacement_id,
            )

        log.info("Profile removed: %s", profile_id)
        return True

    def set_active(self, profile_id: str) -> None:
        if self.get_profile(profile_id) is None:
            raise ValueError(f"Unknown profile: {profile_id!r}")

        self._storage.ensure_profile_dirs(profile_id)
        self._preferences.set_last_active_profile(profile_id)
        self._active_id = profile_id
        log.info("Active profile: %s", profile_id)

    def restore_last_active(self) -> str | None:
        if not self._profiles:
            return None
        last_active = self._preferences.last_active_profile()
        if last_active and self.get_profile(last_active) is not None:
            return last_active
        return self._profiles[0].id

    def migrate_legacy(self, profile_name: str) -> ProfileInfo:
        profile = self.create_profile(profile_name)
        try:
            self._storage.copy_legacy_data(profile.id)
            self._preferences.copy_legacy_settings(profile.id)
        except Exception:  # noqa: BLE001 - legacy migration transaction boundary
            self.rollback_profile_creation(profile.id)
            raise
        self._storage.cleanup_legacy_data()
        self._preferences.cleanup_legacy_settings()
        log.info("Legacy profile migration completed for %s", profile.id)
        return profile

    def prepare_profile_creation(self) -> None:
        if self._active_id:
            self._preferences.prepare_profile_creation(self._active_id)

    def remember_profile_for_next_launch(self, profile_id: str) -> None:
        if self.get_profile(profile_id) is None:
            raise ValueError(f"Unknown profile: {profile_id!r}")
        self._preferences.set_last_active_profile(profile_id)

    def rollback_profile_creation(self, profile_id: str) -> None:
        if self.get_profile(profile_id) is None:
            return
        updated = [
            profile for profile in self._profiles if profile.id != profile_id
        ]
        self._registry.save(updated)
        self._profiles = updated
        self._storage.delete_profile_data(profile_id)
        self._preferences.clear_profile_settings(profile_id)
        if self._active_id == profile_id:
            self._active_id = ""
