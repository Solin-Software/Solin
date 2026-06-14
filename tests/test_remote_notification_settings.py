from __future__ import annotations

import uuid

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.remote.notification_settings import NotificationSettingsStore


def test_notification_settings_store_roundtrips_seen_ids() -> None:
    store = NotificationSettingsStore.for_organization(
        f"SolinTest_notifications_{uuid.uuid4().hex}",
    )
    store.settings.clear()
    try:
        store.mark_seen("notice-1")
        store.mark_seen("notice-2")
        store.mark_seen("notice-1")

        assert store.seen_ids() == {"notice-1", "notice-2"}

        store.reset_seen_ids()

        assert store.seen_ids() == set()
    finally:
        store.settings.clear()


def test_notification_settings_store_handles_corrupt_seen_ids() -> None:
    store = NotificationSettingsStore.for_organization(
        f"SolinTest_notifications_{uuid.uuid4().hex}",
    )
    store.settings.clear()
    try:
        store.settings.set_value(SettingsKey.NOTIFICATIONS_SEEN_IDS, "{broken")

        assert store.seen_ids() == set()

        store.mark_seen("notice-1")

        assert store.seen_ids() == {"notice-1"}
    finally:
        store.settings.clear()


def test_notification_settings_store_caps_seen_history() -> None:
    store = NotificationSettingsStore.for_organization(
        f"SolinTest_notifications_{uuid.uuid4().hex}",
    )
    store.settings.clear()
    try:
        for value in ("a", "b", "c"):
            store.mark_seen(value, limit=2)

        assert store.seen_ids() == {"b", "c"}
    finally:
        store.settings.clear()
