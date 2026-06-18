from __future__ import annotations

import json
from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_NOTIFICATIONS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class NotificationSettingsStore:
    """Typed profile-scoped settings for remote notification state."""

    settings: SettingsStore

    @classmethod
    def for_organization(cls, organization: str) -> "NotificationSettingsStore":
        return cls(SettingsStore.for_namespace(organization, QSETTINGS_NOTIFICATIONS_APP))

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings) -> "NotificationSettingsStore":
        return cls.for_organization(profile_settings.organization)

    def seen_ids(self) -> set[str]:
        raw = self.settings.string(SettingsKey.NOTIFICATIONS_SEEN_IDS, "[]")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return set()
        if not isinstance(data, list):
            return set()
        return {str(item) for item in data}

    def mark_seen(self, notification_id: str, *, limit: int = 500) -> None:
        raw = self.settings.string(SettingsKey.NOTIFICATIONS_SEEN_IDS, "[]")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = []
        if not isinstance(data, list):
            data = []

        if notification_id not in data:
            data.append(notification_id)
            if len(data) > limit:
                data = data[-limit:]
            self.settings.set_value(SettingsKey.NOTIFICATIONS_SEEN_IDS, json.dumps(data))

    def reset_seen_ids(self) -> None:
        self.settings.set_value(SettingsKey.NOTIFICATIONS_SEEN_IDS, "[]")
