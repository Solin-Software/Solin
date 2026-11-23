from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QProcess, QSettings

from solin.core.foundation.settings_store import SettingsStore
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.integrations.automation.zoom.protocol import (
    PLAYBACK_MICROPHONE,
    PROTOCOL_VERSION,
    RAISED_HANDS,
)
from solin.core.integrations.automation.zoom.service import ZoomService, _Request
from solin.core.integrations.automation.zoom.state import (
    ParticipantAction,
    ParticipantStepResult,
    ParticipantStepStatus,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
    SequenceResult,
    StepFailure,
)


@pytest.fixture
def zoom_settings(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs.ini")
    monkeypatch.setattr(SettingsStore, "_settings", lambda _: QSettings(path, QSettings.Format.IniFormat))
    return ZoomSettingsStore(SettingsStore.for_namespace("test", "prefs"))


@pytest.fixture
def service(zoom_settings):
    service = ZoomService(zoom_settings)
    yield service
    service.stop(wait=True)
    service.deleteLater()


def test_sequences_are_coalesced_and_wait_for_current_probe(service, monkeypatch):
    service._active = True
    service._connected = True
    service._pending = _Request(1, "meeting")
    calls = []
    monkeypatch.setattr(service, "_send", calls.append)
    assert service.request_pre_meeting()
    assert not service.request_pre_meeting()
    assert not service.request_post_meeting()
    assert calls == []
    service._ready = True
    service._receive({"id": 1, "connected": True})
    assert calls == ["pre_meeting"]


def test_old_process_cannot_publish_after_disable(service):
    service._active = True
    service._connected = True
    service._ready = True
    service._pending = _Request(8, "pre_meeting")
    results = []
    service.operation_finished.connect(results.append)
    service.stop()
    service._read(object())
    assert results == [SequenceResult("pre_meeting", cancelled=True)]
    assert not service.is_connected
    assert not service.is_busy


def test_timeout_reports_uncertain_effect_without_repeating_action(service):
    service._active = True
    service._ready = True
    service._pending = _Request(2, "post_meeting")
    results = []
    service.operation_finished.connect(results.append)
    service._timed_out()
    assert results[0].failed_steps[0].code == "worker_timeout"
    assert not service.is_busy


def test_partial_results_are_delivered_once(service):
    service._active = service._ready = True
    service._pending = _Request(3, "pre_meeting")
    results = []
    service.operation_finished.connect(results.append)
    service._receive(
        {
            "id": 3,
            "result": {
                "sequence": "pre_meeting",
                "cancelled": False,
                "failed_steps": [{"step": "video_on", "code": "video_not_changed"}],
            },
        }
    )
    assert len(results) == 1
    assert not results[0].ok
    assert service._pending is None


def test_missing_meeting_result_immediately_disables_connection_state(service):
    service._active = service._ready = service._connected = True
    service._pending = _Request(3, "pre_meeting")
    service._receive({"id": 3, "error": "meeting_not_found"})
    assert not service.is_connected
    assert service.is_enabled


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b'{"id":999,"result":{}}\n',
        b'{"id":3,"result":[]}\n',
        b'{"id":3,"result":{"sequence":"pre_meeting","cancelled":false,"failed_steps":null}}\n',
    ],
)
def test_invalid_worker_reply_completes_pending_operation_as_failure(service, payload):
    service._active = service._ready = True
    service._pending = _Request(3, "pre_meeting")
    retired = []
    process = SimpleNamespace(
        readAllStandardOutput=lambda: QByteArray(payload),
        state=lambda: QProcess.ProcessState.NotRunning,
        deleteLater=lambda: retired.append(True),
    )
    service._process = process
    results = []
    service.operation_finished.connect(results.append)
    service._read(process)
    assert len(results) == 1 and not results[0].ok
    assert results[0].failed_steps[0].code == "worker_failed"
    assert retired == [True]
    assert service._pending is None and not service.is_busy


def test_repeated_worker_failures_back_off_and_success_resets_delay(service):
    service._active = True
    for _ in range(8):
        service._retire(code="worker_failed")
    assert service._poll.interval() == 30000
    service._ready = True
    service._pending = _Request(1, "meeting")
    service._receive({"id": 1, "connected": False})
    assert service._poll.interval() == 1000
    assert service._restart_delay_ms == 1000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows worker discovery")
def test_worker_startup_and_discovery_are_read_only(service):
    connections = []
    errors = []
    service.connection_changed.connect(connections.append)
    service.error.connect(errors.append)
    service.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (not service._ready or service._pending is not None):
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert service._ready, errors
    assert service._pending is None
    assert service._process is not None
    # The persistent worker rejects commands outside the meeting automation API.
    process = service._process
    process.write(json.dumps({"id": 500, "command": "start_share"}).encode() + b"\n")
    process.waitForFinished(2000)
    assert process.exitCode() == 2


@pytest.fixture
def monitor(zoom_settings):
    service = ZoomService(zoom_settings, raised_hands_enabled=True)
    # Drive the real supervisor using an in-memory process queue.
    messages = []
    service._active = service._ready = True
    service._process = SimpleNamespace(
        write=lambda payload: messages.append(json.loads(payload)) or len(payload),
        state=lambda: QProcess.ProcessState.NotRunning,
        deleteLater=lambda: None,
        waitForFinished=lambda _: True,
    )
    yield service, messages
    service.stop(wait=True)
    service.deleteLater()


def _reply(service, messages, **fields):
    service._receive({"id": messages[-1]["id"], **fields})


_PLAYBACK_OWNER = [123, 456]


def test_playback_microphone_reconciles_latest_state_after_pending_commands(
    monitor, monkeypatch, zoom_settings,
):
    monkeypatch.setattr(ZoomService, "is_available", property(lambda _: True))
    service, messages = monitor
    service.refresh()
    service.set_media_playing(True)
    service.set_mute_during_playback(True)
    assert [message["command"] for message in messages] == ["meeting"]

    _reply(service, messages, connected=True)
    assert messages[-1]["command"] == PLAYBACK_MICROPHONE
    assert messages[-1]["muted"] is True
    assert not service.is_busy

    service.set_media_playing(False)
    service.set_media_playing(True)
    service.set_media_playing(False)
    assert len(messages) == 2
    _reply(service, messages, owner=_PLAYBACK_OWNER)
    assert messages[-1]["muted"] is False
    _reply(service, messages, owner=None)
    assert len(messages) == 3

    service.set_media_playing(True)
    assert messages[-1]["muted"] is True
    _reply(service, messages, owner=_PLAYBACK_OWNER)
    assert zoom_settings.mute_during_playback()
    service.set_mute_during_playback(False)
    assert messages[-1]["muted"] is False
    _reply(service, messages, owner=None)
    assert not zoom_settings.mute_during_playback()


@pytest.mark.parametrize("playing_after_restart", [False, True])
def test_cancelling_another_action_preserves_playback_mute_until_restored(
    monitor, monkeypatch, playing_after_restart,
):
    monkeypatch.setattr(ZoomService, "is_available", property(lambda _: True))
    service, messages = monitor
    service.refresh()
    _reply(service, messages, connected=True)
    service.set_mute_during_playback(True)
    service.set_media_playing(True)
    _reply(service, messages, owner=_PLAYBACK_OWNER)

    worker = service._process
    killed = []
    worker.kill = lambda: killed.append(True)
    worker.state = lambda: QProcess.ProcessState.Running
    results = []
    service.operation_finished.connect(results.append)
    assert service.request_pre_meeting()
    service.cancel()
    assert killed == [True]
    assert results == [SequenceResult("pre_meeting", cancelled=True)]

    if not playing_after_restart:
        service.set_media_playing(False)
    replacement = SimpleNamespace(
        write=worker.write,
        state=lambda: QProcess.ProcessState.NotRunning,
        deleteLater=lambda: None,
        waitForFinished=lambda _: True,
    )
    monkeypatch.setattr(service, "_launch", lambda: setattr(service, "_process", replacement))
    service.refresh()
    service._receive({"ready": PROTOCOL_VERSION})
    _reply(service, messages, connected=True)

    if playing_after_restart:
        assert messages[-1]["muted"] is True
        assert messages[-1]["owner"] == _PLAYBACK_OWNER
        _reply(service, messages, owner=_PLAYBACK_OWNER)
        service.set_media_playing(False)
    assert messages[-1]["command"] == PLAYBACK_MICROPHONE
    assert messages[-1]["muted"] is False
    assert messages[-1]["owner"] == _PLAYBACK_OWNER
    _reply(service, messages, owner=None)

    service.set_media_playing(True)
    assert messages[-1]["owner"] is None


def test_zoom_disable_releases_playback_worker_before_restarting(monitor, monkeypatch):
    monkeypatch.setattr(ZoomService, "is_available", property(lambda _: True))
    service, messages = monitor
    service.refresh()
    _reply(service, messages, connected=True)
    service.set_mute_during_playback(True)
    service.set_media_playing(True)
    _reply(service, messages, owner=_PLAYBACK_OWNER)

    worker = service._process
    events = []
    worker.state = lambda: QProcess.ProcessState.Running
    worker.closeWriteChannel = lambda: events.append("eof")
    monkeypatch.setattr(service, "_launch", lambda: events.append("launch"))
    service.stop()
    service.start()
    assert events == ["eof"]
    service._exited(worker)
    assert events == ["eof", "launch"]


_REFERENCE = "12345678" * 4


def _ready():
    return {
        "status": "ready",
        "hands": [{"name": "Participant", "audio": "muted", "reference": _REFERENCE}],
        "chronological": True,
        "error": None,
    }


def test_raised_hands_flag_defaults_to_off(service):
    service._active = True
    assert not service.set_raised_hands_monitoring(True)
    assert service.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE


def test_monitoring_preference_is_restored_on_service_start(monkeypatch, zoom_settings):
    monkeypatch.setattr(ZoomService, "is_available", property(lambda _: True))
    monkeypatch.setattr(ZoomService, "refresh", lambda self: None)
    first = ZoomService(zoom_settings, raised_hands_enabled=True)
    first.start()
    try:
        assert first.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE
        assert first.set_raised_hands_monitoring(True)
    finally:
        first.stop()
    assert first.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE

    restored = ZoomService(zoom_settings, raised_hands_enabled=True)
    restored.start()
    try:
        assert restored.raised_hands_snapshot.status is RaisedHandsStatus.NO_MEETING
        assert restored.set_raised_hands_monitoring(False)
    finally:
        restored.stop()

    restored.start()
    try:
        assert restored.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE
    finally:
        restored.stop()


def test_raised_hands_opens_once_then_pauses_and_resumes_passively(monitor):
    service, messages = monitor
    assert service.set_raised_hands_monitoring(True)
    assert service.set_raised_hands_monitoring(True)
    assert len(messages) == 1
    assert messages[-1]["command"] == "meeting"
    _reply(service, messages, connected=True)
    assert messages[-1]["command"] == RAISED_HANDS
    assert messages[-1]["open_panel"] is True

    _reply(service, messages, snapshot=_ready())
    assert service._poll.interval() == 1500
    assert service.raised_hands_snapshot.hands
    service.refresh()
    assert messages[-1]["open_panel"] is False
    before = len(messages)
    service.refresh()
    assert len(messages) == before
    _reply(service, messages, error="panel_closed")
    assert service._poll.interval() == 5000
    assert service.raised_hands_snapshot == RaisedHandsSnapshot(RaisedHandsStatus.PANEL_CLOSED)
    assert not service.request_participant_action(ParticipantAction.LOWER_HAND, _REFERENCE)

    service.refresh()
    assert messages[-1]["open_panel"] is False
    _reply(service, messages, snapshot=_ready())
    assert service._poll.interval() == 1500
    assert service.raised_hands_snapshot.hands


def test_monitor_discards_previous_activation_reply(monitor):
    service, messages = monitor
    service._set_connected(True)
    assert service.set_raised_hands_monitoring(True)
    assert service.set_raised_hands_monitoring(False)
    assert service.raised_hands_snapshot.status is RaisedHandsStatus.INACTIVE
    assert service.set_raised_hands_monitoring(True)
    _reply(service, messages, snapshot=_ready())
    assert not service.raised_hands_snapshot.hands
    assert messages[-1]["open_panel"] is True


def test_monitor_does_not_reopen_after_losing_a_meeting(monitor):
    service, messages = monitor
    service._set_connected(True)
    assert service.set_raised_hands_monitoring(True)
    _reply(service, messages, error="meeting_not_found")
    assert service.raised_hands_snapshot.status is RaisedHandsStatus.NO_MEETING
    service.refresh()
    assert messages[-1]["command"] == "meeting"
    _reply(service, messages, connected=True)
    service.refresh()
    assert messages[-1]["command"] == RAISED_HANDS
    assert messages[-1]["open_panel"] is False


@pytest.mark.parametrize("lost_target", [False, True])
def test_participant_action_queues_behind_probe_and_checks_latest_target(monitor, lost_target):
    service, messages = monitor
    service._set_connected(True)
    assert service.set_raised_hands_monitoring(True)
    _reply(service, messages, snapshot=_ready())
    service.refresh()
    assert service.request_participant_action(ParticipantAction.LOWER_HAND, _REFERENCE)
    assert not service.request_pre_meeting()
    assert not service.request_participant_action(ParticipantAction.REQUEST_UNMUTE, _REFERENCE)
    assert messages[-1]["command"] == RAISED_HANDS
    results = []
    service.participant_action_finished.connect(results.append)

    if lost_target:
        _reply(service, messages, error="panel_closed")
        assert messages[-1]["command"] == RAISED_HANDS
        assert results[0].failure.code == "participant_changed"
        assert not service.is_busy
    else:
        _reply(service, messages, snapshot=_ready())
        assert messages[-1]["command"] == "lower_hand"
        assert messages[-1]["reference"] == _REFERENCE
        assert service.is_busy


def test_worker_timeout_keeps_verified_participant_steps(monitor):
    service, messages = monitor
    service._set_connected(True)
    assert service.set_raised_hands_monitoring(True)
    _reply(service, messages, snapshot=_ready())
    assert service.request_participant_action(ParticipantAction.MUTE_AND_LOWER, _REFERENCE)
    _reply(service, messages, participant_step={"step": "mute", "status": "changed"})
    results = []
    service.participant_action_finished.connect(results.append)

    service._timed_out()

    assert not service.is_busy
    assert service.raised_hands_snapshot == RaisedHandsSnapshot(
        RaisedHandsStatus.UNAVAILABLE, error="worker_timeout"
    )
    assert len(results) == 1
    assert results[0].completed_steps == (
        ParticipantStepResult("mute", ParticipantStepStatus.CHANGED),
    )
    assert results[0].failure == StepFailure("worker", "worker_timeout")
    assert [r["command"] for r in messages].count("mute_and_lower") == 1
