from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from PySide6.QtCore import QDate, QDateTime, QTime

from solin.controllers.media_countdown_automation_controller import (
    MediaCountdownAutomationSnapshot,
)
from solin.core.meetings.schedule import (
    MIDWEEK,
    WEEKEND,
    MeetingOccurrence,
    MeetingSchedule,
    MeetingSlot,
)
from solin.core.timer.media_countdown_automation import (
    MediaCountdownAutomationConfig,
    MediaCountdownAutomationStatus,
    MediaCountdownBlockingReason,
)
from solin.core.timer.media_countdown_settings import MediaCountdownSettings
from solin.core.timer.models import MediaCountdownPresentation
from solin.ui.qml.timer_bridge import TimerBridge


class _Automation:
    def __init__(self, snapshot: MediaCountdownAutomationSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> MediaCountdownAutomationSnapshot:
        return self._snapshot

    def suggested_manual_target(self) -> tuple[int, int, bool]:
        return 19, 30, True


class _SignalRecorder:
    def __init__(self) -> None:
        self.emissions: list[tuple[QDateTime, str]] = []

    def emit(self, target: QDateTime, presentation: str) -> None:
        self.emissions.append((target, presentation))


def test_media_countdown_read_model_separates_shared_and_contextual_state() -> None:
    local_timezone = datetime.now().astimezone().tzinfo
    starts_at = datetime(2026, 7, 20, 19, 30, tzinfo=local_timezone)
    midweek = MeetingSlot(MIDWEEK, starts_at.weekday(), 19 * 60 + 30)
    schedule = MeetingSchedule(midweek=midweek, weekend=MeetingSlot(WEEKEND))
    occurrence = MeetingOccurrence(midweek, starts_at)
    snapshot = MediaCountdownAutomationSnapshot(
        settings=MediaCountdownSettings(
            presentation=MediaCountdownPresentation.YEARLY_TEXT,
            automation=MediaCountdownAutomationConfig(
                enabled=True,
                lead_seconds=600,
            ),
        ),
        schedule=schedule,
        status=MediaCountdownAutomationStatus.READY,
        next_occurrence=occurrence,
        has_projection_target=True,
        active_countdown=True,
        active_origin="manual",
        active_target=QDateTime(QDate(2026, 7, 20), QTime(19, 30)),
        active_automatic=False,
        blocking_reason=MediaCountdownBlockingReason.NONE,
    )
    bridge = SimpleNamespace(_media_countdown_automation=_Automation(snapshot))

    model = TimerBridge._media_countdown_model(bridge)

    assert set(model) == {
        "presentationIndex",
        "activeProjection",
        "manualSuggestion",
        "automation",
        "schedule",
    }
    assert model["presentationIndex"] == 1
    assert model["activeProjection"] == {
        "active": True,
        "origin": "manual",
        "targetTime": "19:30",
    }
    assert model["manualSuggestion"] == {
        "hour": 19,
        "minute": 30,
        "fromMeeting": True,
    }
    assert model["automation"] == {
        "enabled": True,
        "leadSeconds": 600,
        "status": "ready",
        "activeAutomatic": False,
        "blockingReason": "",
    }
    assert model["schedule"]["configuredCount"] == 1
    assert model["schedule"]["next"]["startTime"] == "19:30"


def test_manual_duration_slot_clamps_the_supported_range_in_seconds() -> None:
    recorder = _SignalRecorder()
    bridge = SimpleNamespace(
        mediaCountdownRequested=recorder,
        _selected_media_countdown_presentation=lambda: "circular",
    )

    before_minimum = QDateTime.currentDateTime()
    TimerBridge.startCountdownDuration(bridge, 1)
    before_maximum = QDateTime.currentDateTime()
    TimerBridge.startCountdownDuration(bridge, 100_000)

    minimum_target, minimum_presentation = recorder.emissions[0]
    maximum_target, maximum_presentation = recorder.emissions[1]
    assert 10 <= before_minimum.secsTo(minimum_target) <= 11
    assert 86_399 <= before_maximum.secsTo(maximum_target) <= 86_400
    assert minimum_presentation == maximum_presentation == "circular"
    assert not hasattr(TimerBridge, "startCountdownMinutes")
