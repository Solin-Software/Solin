"""Runtime orchestration for meeting-aware media countdown projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
    QObject,
    QTimer,
    QT_TRANSLATE_NOOP,
    Signal,
)

from solin.core.meetings.schedule import MeetingOccurrence, MeetingSchedule
from solin.core.timer.media_countdown_automation import (
    MediaCountdownBlockingReason,
    MediaCountdownAutomationStatus,
)
from solin.core.timer.media_countdown_settings import (
    MediaCountdownSettings,
    MediaCountdownSettingsStore,
)
from solin.core.timer.models import MediaCountdownPresentation

_OUTSIDE_WINDOW_RECHECK_MS = 30_000
_BLOCKED_RECHECK_MS = 1_000
_MISSED_STATUS_SECONDS = 60
_TR_CONTEXT = "MediaCountdownAutomation"
_STARTED_MESSAGE = QT_TRANSLATE_NOOP(
    "MediaCountdownAutomation",
    "Countdown started automatically for {time}.",
)
_IN_USE_MESSAGE = QT_TRANSLATE_NOOP(
    "MediaCountdownAutomation",
    "The countdown did not start because the media window remained in use.",
)
_NO_WINDOW_MESSAGE = QT_TRANSLATE_NOOP(
    "MediaCountdownAutomation",
    "The countdown did not start because no media window was available.",
)
_AUTOMATION_UNAVAILABLE_MESSAGE = QT_TRANSLATE_NOOP(
    "MediaCountdownAutomation",
    "The countdown did not start because automatic projection remained unavailable.",
)
class MeetingScheduleSource(Protocol):
    def load(self) -> MeetingSchedule: ...


@dataclass(frozen=True, slots=True)
class MediaCountdownAutomationSnapshot:
    settings: MediaCountdownSettings
    schedule: MeetingSchedule
    status: MediaCountdownAutomationStatus
    next_occurrence: MeetingOccurrence | None
    has_projection_target: bool
    active_countdown: bool
    active_origin: str
    active_target: QDateTime | None
    active_automatic: bool
    blocking_reason: MediaCountdownBlockingReason


class MediaCountdownAutomationController(QObject):
    """Evaluates configured meeting windows and requests projection at most once."""

    countdown_requested = Signal(QDateTime, str, str)
    automatic_stop_requested = Signal()
    state_changed = Signal()

    def __init__(
        self,
        *,
        settings: MediaCountdownSettingsStore,
        schedule_source: MeetingScheduleSource,
        projection_session: Any,
        playback_protection: Any,
        notifications: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._schedule_source = schedule_source
        self._projection_session = projection_session
        self._playback_protection = playback_protection
        self._notifications = notifications
        self._now_provider = now_provider or (lambda: datetime.now().astimezone())

        self._media_settings = self._settings.load()
        self._config = self._media_settings.automation
        self._schedule = self._schedule_source.load()
        self._status = MediaCountdownAutomationStatus.DISABLED
        self._next_occurrence: MeetingOccurrence | None = None
        self._tracked_occurrence: MeetingOccurrence | None = None
        self._active_occurrence_id = ""
        self._active_occurrence: MeetingOccurrence | None = None
        self._active_starts_at: datetime | None = None
        self._manual_satisfied_occurrence_id = ""
        self._controlled_stop = False
        self._session_suppressed_occurrence_id = ""
        self._yielding_to_projection = False
        self._satisfied_occurrence_ids: set[str] = set()
        self._blocked_reasons: dict[str, MediaCountdownBlockingReason] = {}
        self._notified_occurrence_ids: set[str] = set()
        self._missed_at: datetime | None = None
        self._last_snapshot: MediaCountdownAutomationSnapshot | None = None
        self._started = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.evaluate)
        self._unsubscribe_projection = self._projection_session.subscribe(
            self._on_projection_changed
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.evaluate()

    def shutdown(self) -> None:
        self._started = False
        self._timer.stop()
        if self._unsubscribe_projection is not None:
            self._unsubscribe_projection()
            self._unsubscribe_projection = None

    def snapshot(self) -> MediaCountdownAutomationSnapshot:
        blocking_reason = MediaCountdownBlockingReason.NONE
        if (
            self._status is MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
            and self._next_occurrence is not None
        ):
            blocking_reason = self._blocked_reasons.get(
                self._next_occurrence.slot_id,
                MediaCountdownBlockingReason.NONE,
            )
        state = self._projection_session.state
        active_countdown = state.get("type") == "timer"
        target = state.get("target_dt")
        active_target = (
            target if isinstance(target, QDateTime) and target.isValid() else None
        )
        active_automatic = (
            active_countdown and state.get("origin") == "automatic_countdown"
        )
        return MediaCountdownAutomationSnapshot(
            settings=self._media_settings,
            schedule=self._schedule,
            status=self._status,
            next_occurrence=self._next_occurrence,
            has_projection_target=self._has_projection_target(),
            active_countdown=active_countdown,
            active_origin=(
                "automatic" if active_automatic else "manual" if active_countdown else ""
            ),
            active_target=active_target,
            active_automatic=(
                self._status is MediaCountdownAutomationStatus.ACTIVE
                and active_automatic
            ),
            blocking_reason=blocking_reason,
        )

    def suggested_manual_target(self) -> tuple[int, int, bool]:
        """Return today's next meeting, or the existing next-full-hour fallback."""

        now = self._now_provider()
        occurrence = self._schedule.pre_meeting_occurrence(now)
        if occurrence is not None:
            return occurrence.starts_at.hour, occurrence.starts_at.minute, True
        return (now.hour + 1) % 24, 0, False

    def set_enabled(self, enabled: bool) -> None:
        self._settings.set_enabled(enabled)
        self.evaluate()

    def set_lead_seconds(self, seconds: int) -> None:
        self._settings.set_lead_seconds(seconds)
        self.evaluate()

    def set_presentation(self, presentation: MediaCountdownPresentation | str) -> None:
        self._settings.set_presentation(presentation)
        self.evaluate()

    def reload_schedule(self) -> None:
        updated_schedule = self._schedule_source.load()
        if self._active_occurrence_id and not self._schedule_contains_active_occurrence(
            updated_schedule
        ):
            active_occurrence_id = self._active_occurrence_id
            self._controlled_stop = True
            self.automatic_stop_requested.emit()
            self._controlled_stop = False
            if self._automatic_projection_is_active(active_occurrence_id):
                QTimer.singleShot(0, self.evaluate)
            else:
                self.evaluate()
            return
        self.evaluate()

    def refresh(self) -> None:
        self.evaluate()

    def evaluate(self) -> None:
        now = self._now_provider()
        self._media_settings = self._settings.load()
        self._config = self._media_settings.automation
        self._schedule = self._schedule_source.load()
        self._record_crossed_occurrence(now)
        occurrence = self._schedule.next_occurrence(now)
        self._next_occurrence = occurrence
        self._clear_stale_session_suppression(occurrence)

        if not self._config.enabled:
            self._status = MediaCountdownAutomationStatus.DISABLED
            self._finish_evaluation(None)
            return

        if self._active_occurrence_id and self._automatic_projection_is_active(
            self._active_occurrence_id
        ):
            if self._active_occurrence is not None:
                self._next_occurrence = self._active_occurrence
            self._status = MediaCountdownAutomationStatus.ACTIVE
            self._finish_evaluation(_OUTSIDE_WINDOW_RECHECK_MS)
            return

        if not self._schedule.has_configured_slot or occurrence is None:
            self._status = MediaCountdownAutomationStatus.SCHEDULE_REQUIRED
            self._finish_evaluation(None)
            return

        self._tracked_occurrence = occurrence
        open_at = occurrence.starts_at - timedelta(seconds=self._config.lead_seconds)
        if self._projection_targets_occurrence(occurrence):
            self._satisfied_occurrence_ids.add(occurrence.slot_id)
            self._manual_satisfied_occurrence_id = occurrence.slot_id
            self._status = MediaCountdownAutomationStatus.ACTIVE
            self._finish_evaluation(self._bounded_delay_ms(now, occurrence.starts_at))
            return

        if self._session_suppressed_occurrence_id == occurrence.slot_id:
            self._status = MediaCountdownAutomationStatus.SUPPRESSED
            self._finish_evaluation(self._bounded_delay_ms(now, occurrence.starts_at))
            return

        if occurrence.slot_id in self._satisfied_occurrence_ids:
            self._session_suppressed_occurrence_id = occurrence.slot_id
            self._status = MediaCountdownAutomationStatus.SUPPRESSED
            self._finish_evaluation(self._bounded_delay_ms(now, occurrence.starts_at))
            return

        if now < open_at:
            self._status = self._ready_or_recently_missed(now)
            self._finish_evaluation(self._bounded_delay_ms(now, open_at))
            return

        if now >= occurrence.starts_at:
            self._mark_missed(occurrence, now)
            self._finish_evaluation(_OUTSIDE_WINDOW_RECHECK_MS)
            return

        if self._yielding_to_projection:
            if self._projection_session.has_active_projection():
                self._blocked_reasons[occurrence.slot_id] = (
                    MediaCountdownBlockingReason.IN_USE
                )
                self._status = MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
                self._finish_evaluation(_BLOCKED_RECHECK_MS)
                return
            self._yielding_to_projection = False

        has_projection_target = self._has_projection_target()
        projection_active = self._projection_session.has_active_projection()
        if not has_projection_target or projection_active:
            if not has_projection_target:
                reason = MediaCountdownBlockingReason.NO_WINDOW
            else:
                reason = MediaCountdownBlockingReason.IN_USE
            self._blocked_reasons[occurrence.slot_id] = reason
            self._status = MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
            self._finish_evaluation(_BLOCKED_RECHECK_MS)
            return

        allowed = self._playback_protection.allow_automatic_projection_change(
            projection_active=False,
        )
        if not allowed:
            self._blocked_reasons[occurrence.slot_id] = (
                MediaCountdownBlockingReason.AUTOMATION_UNAVAILABLE
            )
            self._status = MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
            self._finish_evaluation(_BLOCKED_RECHECK_MS)
            return

        self._status = MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
        target = QDateTime.fromSecsSinceEpoch(int(occurrence.starts_at.timestamp()))
        self.countdown_requested.emit(
            target,
            self._media_settings.presentation.value,
            occurrence.slot_id,
        )
        if (
            self._status is not MediaCountdownAutomationStatus.ACTIVE
            and self._blocked_reasons.get(occurrence.slot_id)
            in {None, MediaCountdownBlockingReason.NO_WINDOW}
        ):
            self._blocked_reasons[occurrence.slot_id] = (
                MediaCountdownBlockingReason.AUTOMATION_UNAVAILABLE
            )
        self._finish_evaluation(_BLOCKED_RECHECK_MS)

    def _on_projection_changed(self) -> None:
        state = self._projection_session.state
        automatic_id = (
            str(state.get("occurrence_id") or "")
            if state.get("origin") == "automatic_countdown"
            else ""
        )
        if automatic_id:
            self._yielding_to_projection = False
            occurrence = self._tracked_occurrence
            previous_active_id = self._active_occurrence_id
            self._active_occurrence_id = automatic_id
            if occurrence is not None and occurrence.slot_id == automatic_id:
                self._active_occurrence = occurrence
                self._active_starts_at = occurrence.starts_at
            elif previous_active_id != automatic_id:
                self._active_occurrence = None
                self._active_starts_at = self._target_datetime_from_state(state)
            elif self._active_starts_at is None:
                self._active_starts_at = self._target_datetime_from_state(state)
            self._satisfied_occurrence_ids.add(automatic_id)
            self._status = MediaCountdownAutomationStatus.ACTIVE
            self._notify_started_once(automatic_id, self._active_starts_at)
            self._publish_if_changed()
            return

        occurrence = self._tracked_occurrence
        if occurrence is not None and self._projection_targets_occurrence(occurrence):
            self._yielding_to_projection = False
            self._active_occurrence_id = ""
            self._active_occurrence = None
            self._active_starts_at = None
            self._manual_satisfied_occurrence_id = occurrence.slot_id
            self._satisfied_occurrence_ids.add(occurrence.slot_id)
            self._status = MediaCountdownAutomationStatus.ACTIVE
            self._publish_if_changed()
            return

        if self._manual_satisfied_occurrence_id:
            manual_id = self._manual_satisfied_occurrence_id
            self._manual_satisfied_occurrence_id = ""
            if occurrence is not None and occurrence.slot_id == manual_id:
                if self._now_provider() < occurrence.starts_at:
                    self._session_suppressed_occurrence_id = manual_id
                    self._status = MediaCountdownAutomationStatus.SUPPRESSED

        if self._active_occurrence_id:
            active_id = self._active_occurrence_id
            starts_at = self._active_starts_at
            self._active_occurrence_id = ""
            self._active_occurrence = None
            self._active_starts_at = None
            if (
                not self._controlled_stop
                and starts_at is not None
                and self._now_provider() < starts_at
            ):
                if self._projection_session.has_active_projection():
                    self._yielding_to_projection = True
                    self._satisfied_occurrence_ids.discard(active_id)
                    self._blocked_reasons[active_id] = (
                        MediaCountdownBlockingReason.IN_USE
                    )
                    self._status = (
                        MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
                    )
                else:
                    self._session_suppressed_occurrence_id = active_id
                    self._status = MediaCountdownAutomationStatus.SUPPRESSED
        if self._controlled_stop:
            self._publish_if_changed()
        elif self._started:
            self.evaluate()
        else:
            self._publish_if_changed()

    def _record_crossed_occurrence(self, now: datetime) -> None:
        previous = self._tracked_occurrence
        if previous is None or now < previous.starts_at:
            return
        self._tracked_occurrence = None
        suppressed = self._session_suppressed_occurrence_id == previous.slot_id
        satisfied = previous.slot_id in self._satisfied_occurrence_ids
        if not suppressed and not satisfied:
            self._mark_missed(previous, now)

    def _mark_missed(self, occurrence: MeetingOccurrence, now: datetime) -> None:
        if self._missed_at is not None and occurrence.slot_id in self._satisfied_occurrence_ids:
            return
        self._missed_at = now
        self._status = MediaCountdownAutomationStatus.MISSED
        blocked_reason = self._blocked_reasons.get(occurrence.slot_id)
        if blocked_reason is not None and self._notifications is not None:
            source = {
                MediaCountdownBlockingReason.NO_WINDOW: _NO_WINDOW_MESSAGE,
                MediaCountdownBlockingReason.IN_USE: _IN_USE_MESSAGE,
                MediaCountdownBlockingReason.AUTOMATION_UNAVAILABLE: (
                    _AUTOMATION_UNAVAILABLE_MESSAGE
                ),
            }[blocked_reason]
            self._notifications.warning(
                QCoreApplication.translate(_TR_CONTEXT, source),
                dedupe_key=f"media-countdown-missed:{occurrence.slot_id}",
            )

    def _ready_or_recently_missed(self, now: datetime) -> MediaCountdownAutomationStatus:
        if self._missed_at is not None:
            elapsed = (now - self._missed_at).total_seconds()
            if 0 <= elapsed < _MISSED_STATUS_SECONDS:
                return MediaCountdownAutomationStatus.MISSED
            self._missed_at = None
        return MediaCountdownAutomationStatus.READY

    def _clear_stale_session_suppression(
        self,
        occurrence: MeetingOccurrence | None,
    ) -> None:
        occurrence_id = occurrence.slot_id if occurrence is not None else ""
        if self._session_suppressed_occurrence_id != occurrence_id:
            self._session_suppressed_occurrence_id = ""

    def _projection_targets_occurrence(self, occurrence: MeetingOccurrence) -> bool:
        state = self._projection_session.state
        if state.get("type") != "timer":
            return False
        target = state.get("target_dt")
        if not isinstance(target, QDateTime) or not target.isValid():
            return False
        return abs(target.toSecsSinceEpoch() - int(occurrence.starts_at.timestamp())) <= 1

    def _automatic_projection_is_active(self, occurrence_id: str) -> bool:
        state = self._projection_session.state
        return (
            state.get("type") == "timer"
            and state.get("origin") == "automatic_countdown"
            and state.get("occurrence_id") == occurrence_id
        )

    def _has_projection_target(self) -> bool:
        return bool(self._projection_session.all_windows())

    def _schedule_contains_active_occurrence(self, schedule: MeetingSchedule) -> bool:
        starts_at = self._active_starts_at
        active = self._active_occurrence
        if starts_at is None or active is None:
            return False
        return any(
            slot.is_configured
            and slot.kind == active.slot.kind
            and slot.weekday == starts_at.weekday()
            and slot.time_text == starts_at.strftime("%H:%M")
            for slot in schedule.slots
        )

    @staticmethod
    def _target_datetime_from_state(state: dict[str, Any]) -> datetime | None:
        target = state.get("target_dt")
        if not isinstance(target, QDateTime) or not target.isValid():
            return None
        return datetime.fromtimestamp(target.toSecsSinceEpoch()).astimezone()

    def _notify_started_once(self, occurrence_id: str, starts_at: datetime | None) -> None:
        if (
            self._notifications is None
            or starts_at is None
            or occurrence_id in self._notified_occurrence_ids
        ):
            return
        self._notified_occurrence_ids.add(occurrence_id)
        message = QCoreApplication.translate(_TR_CONTEXT, _STARTED_MESSAGE).format(
            time=starts_at.strftime("%H:%M")
        )
        self._notifications.information(
            message,
            dedupe_key=f"media-countdown-started:{occurrence_id}",
        )

    def _finish_evaluation(self, delay_ms: int | None) -> None:
        self._publish_if_changed()
        if not self._started or delay_ms is None:
            self._timer.stop()
            return
        self._timer.start(max(1, int(delay_ms)))

    def _publish_if_changed(self) -> None:
        snapshot = self.snapshot()
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        self.state_changed.emit()

    @staticmethod
    def _bounded_delay_ms(now: datetime, target: datetime) -> int:
        delay_ms = max(1, int((target - now).total_seconds() * 1000))
        return min(_OUTSIDE_WINDOW_RECHECK_MS, delay_ms)


__all__ = [
    "MediaCountdownAutomationController",
    "MediaCountdownAutomationSnapshot",
]
