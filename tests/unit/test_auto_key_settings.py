import json
import uuid

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.integrations.automation.auto_key_actions import (
    AutoKeyAction,
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
    EVENT_MEDIA_STARTED,
)
from solin.core.integrations.automation.shortcuts import AutoKeySettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.ui.auto_key_labels import auto_key_event_label


def _store() -> tuple[AutoKeySettingsStore, SettingsStore]:
    profile_settings = ProfileSettings.for_profile_id(
        f"auto_key_settings_{uuid.uuid4().hex}"
    )
    settings = SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )
    return AutoKeySettingsStore(settings), settings


def test_auto_key_settings_roundtrips_enabled_state_and_actions():
    store, settings = _store()
    settings.clear()
    try:
        assert store.is_enabled() is False

        store.set_enabled(True)
        store.save_actions(
            [
                AutoKeyAction(
                    id="start",
                    event=EVENT_MEDIA_STARTED,
                    sequence="Ctrl+Alt+1",
                ),
                AutoKeyAction(
                    id="end",
                    event=EVENT_MEDIA_ENDED,
                    sequence="Ctrl+Alt+2",
                    enabled=False,
                ),
            ]
        )

        assert store.is_enabled() is True
        assert store.actions() == [
            AutoKeyAction(
                id="start",
                event=EVENT_MEDIA_STARTED,
                sequence="Ctrl+Alt+1",
            ),
            AutoKeyAction(
                id="end",
                event=EVENT_MEDIA_ENDED,
                sequence="Ctrl+Alt+2",
                enabled=False,
            ),
        ]
        assert store.action_count_for_event(EVENT_MEDIA_STARTED) == 1
        assert store.action_count_for_event(EVENT_MEDIA_ENDED) == 0
    finally:
        settings.clear()


def test_auto_key_settings_ignores_invalid_serialized_actions():
    store, settings = _store()
    settings.clear()
    try:
        settings.set_value(
            SettingsKey.AUTO_KEYS_ACTIONS,
            json.dumps(
                [
                    {"event": EVENT_MEDIA_STARTED, "sequence": "Ctrl+1"},
                    {"event": "unknown", "sequence": "Ctrl+2"},
                    {"event": EVENT_MEDIA_ENDED, "sequence": ""},
                    "not-a-dict",
                ]
            ),
        )

        actions = store.actions()

        assert len(actions) == 1
        assert actions[0].event == EVENT_MEDIA_STARTED
        assert actions[0].sequence == "Ctrl+1"
    finally:
        settings.clear()


def test_auto_key_event_labels_render_at_ui_boundary():
    assert auto_key_event_label(EVENT_MEDIA_STARTED) == "Media starts"
    assert auto_key_event_label(EVENT_MEDIA_ENDED) == "Media ends"
    assert auto_key_event_label(EVENT_MEDIA_PAUSED) == "Video pauses"
    assert auto_key_event_label(EVENT_MEDIA_RESUMED) == "Video resumes"
    assert auto_key_event_label("custom") == "custom"
