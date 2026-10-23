from __future__ import annotations

import uuid

import pytest
from PySide6.QtCore import QObject, Signal

from solin.core.conference.settings import HostedMeetingSettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.ui.qml.settings.conference import ConferenceSettings


class _Switch:
    def __init__(self):
        self.active = []

    def set_active(self, key):
        self.active.append(key)


class _Lister(QObject):
    finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = 0

    def start(self):
        self.started += 1


@pytest.fixture
def store():
    store = HostedMeetingSettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"conference_domain_{uuid.uuid4().hex}"))
    yield store
    store.settings.clear()


def _domain(store, available=True):
    listers = []

    def factory(parent):
        listers.append(_Lister(parent))
        return listers[-1]

    switch = _Switch()
    domain = ConferenceSettings(store, switch, hosting_available=available,
                                device_lister_factory=factory)
    return domain, switch, listers[0]


def test_choosing_solin_switches_the_provider_and_lists_devices(store):
    domain, switch, lister = _domain(store)
    assert domain.state["meetingProvider"] == "zoom" and not domain.state["hostedSelected"]
    domain.refresh_runtime()
    assert lister.started == 0  # no device probing while Zoom hosts
    domain.setValue("meetingProvider", "solin")
    assert switch.active == ["solin"] and domain.state["hostedSelected"]
    domain.refresh_runtime()
    assert lister.started == 1


def test_hosting_cannot_be_chosen_without_the_server(store):
    domain, switch, _lister = _domain(store, available=False)
    domain.setValue("meetingProvider", "solin")
    assert switch.active == [] and store.provider().value == "zoom"


def test_device_options_offer_default_and_devices_with_ids(store):
    domain, _switch, lister = _domain(store)
    lister.finished.emit({"recording": [{"id": "{in}", "name": "Mixer In"},
                                        {"id": "", "name": "No id"}],
                          "playout": []})
    options = domain.state["meetingMicrophoneOptions"]
    assert [o["value"] for o in options] == ["", "{in}"]
    domain.setValue("meetingSpeaker", "{gone}")
    assert store.hall_audio().playout_device == "{gone}"
    speaker = domain.state["meetingSpeakerOptions"]
    assert [o["value"] for o in speaker] == ["", "{gone}"]  # kept, marked as not connected


def test_hall_audio_toggles_are_saved(store):
    domain, _switch, _lister = _domain(store)
    domain.setValue("meetingHallAudio", False)
    domain.setValue("meetingNoiseSuppression", False)
    hall = store.hall_audio()
    assert not hall.enabled and not hall.noise_suppression and hall.echo_cancellation
    assert domain.state["meetingHallAudio"] is False
    with pytest.raises(ValueError):
        domain.setValue("meetingVolume", 3)
