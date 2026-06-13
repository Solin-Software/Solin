"""Immutable profile-scoped QSettings namespace."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings

from solin.core.foundation.constants import (
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
)
from solin.core.foundation.settings_store import ProfileAppSettingsStore


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    profile_id: str
    organization: str

    @classmethod
    def for_profile_id(cls, profile_id: str) -> ProfileSettings:
        normalized = profile_id.strip()
        if not normalized:
            raise ValueError("Profile settings require a non-empty profile id.")
        return cls(
            profile_id=normalized,
            organization=f"{QSETTINGS_PROFILE_ORG_PREFIX}{normalized}",
        )

    def prefs(self, app_name: str = QSETTINGS_PREFS_APP) -> QSettings:
        return QSettings(self.organization, app_name)

    def app_settings(self) -> ProfileAppSettingsStore:
        return ProfileAppSettingsStore.for_organization(self.organization)
