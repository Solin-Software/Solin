from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QProcess

from solin.core.integrations.automation.zoom.service import ZoomService, _Request
from solin.core.conference.provider import SequenceResult


@pytest.fixture
def service():
    service = ZoomService(None)
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
    assert service._queued_sequence is None


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
    # The worker remains persistent and only accepts whitelisted audio/video workflows.
    process = service._process
    process.write(json.dumps({"id": 500, "command": "start_share"}).encode() + b"\n")
    process.waitForFinished(2000)
    assert process.exitCode() == 2
