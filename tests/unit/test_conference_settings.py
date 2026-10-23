from __future__ import annotations

import uuid

import pytest

from solin.core.conference.settings import (
    DEFAULT_HALL_NAME,
    HostedMeetingSettingsStore,
    MeetingProviderChoice,
    MeetingQuality,
)
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.profiles.settings import ProfileSettings


@pytest.fixture
def store():
    store = HostedMeetingSettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"conference_settings_{uuid.uuid4().hex}")
    )
    yield store
    store.settings.clear()


def test_defaults_keep_zoom_and_a_safe_meeting(store):
    assert store.provider() is MeetingProviderChoice.ZOOM
    assert store.hall_name() == DEFAULT_HALL_NAME
    assert store.quality() is MeetingQuality.SD
    assert store.e2ee() and not store.attendee_video()


def test_values_round_trip_and_bad_values_fall_back(store):
    store.set_provider("solin")
    store.set_hall_name("  Salão Central  ")
    store.set_quality(MeetingQuality.AUDIO_ONLY)
    store.set_e2ee(False)
    store.set_attendee_video(True)
    assert store.provider() is MeetingProviderChoice.SOLIN
    assert store.hall_name() == "Salão Central"
    assert store.quality() is MeetingQuality.AUDIO_ONLY
    assert not store.e2ee() and store.attendee_video()

    store.settings.set_value(SettingsKey.MEETING_PROVIDER, "teams")
    store.settings.set_value(SettingsKey.MEETING_QUALITY, "4k")
    store.set_hall_name("   ")
    assert store.provider() is MeetingProviderChoice.ZOOM
    assert store.quality() is MeetingQuality.SD
    assert store.hall_name() == DEFAULT_HALL_NAME
    with pytest.raises(ValueError):
        store.set_provider("teams")


def test_meeting_settings_follow_preferences_with_fresh_credentials(store):
    store.set_quality(MeetingQuality.HD)
    store.set_attendee_video(True)
    first, second = store.meeting_settings(), store.meeting_settings()
    assert (first.width, first.height) == (1280, 720) and not first.audio_only
    assert first.e2ee and first.policy.attendee_video
    assert first.server.api_secret != second.server.api_secret
    store.set_quality(MeetingQuality.AUDIO_ONLY)
    assert store.meeting_settings().audio_only


def test_hall_audio_preferences_round_trip(store):
    hall = store.hall_audio()
    assert hall.enabled and hall.echo_cancellation and hall.noise_suppression
    assert hall.recording_device == "" and hall.playout_device == ""
    store.set_hall_audio(enabled=False, recording_device="{in}", noise_suppression=False)
    hall = store.hall_audio()
    assert not hall.enabled and hall.recording_device == "{in}" and not hall.noise_suppression
    assert store.meeting_settings().hall_audio == hall
    with pytest.raises(ValueError):
        store.set_hall_audio(volume=3)
