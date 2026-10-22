"""Context object for the Solin-hosted meeting console (``MeetingConsoleView.qml``)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication

from solin.core.conference.meeting_core import MeetingParticipant, ParticipantStatus
from solin.core.conference.meeting_service import MeetingState
from solin.core.conference.settings import MeetingProviderChoice


def participant_row(participant: MeetingParticipant, hand_position: int = 0) -> dict[str, Any]:
    return {
        "identity": participant.identity,
        "name": participant.name,
        "status": participant.status.value,
        "attendance": participant.attendance,
        "handRaised": participant.hand_raised,
        "handPosition": hand_position,
        "microphoneLive": participant.microphone_live,
    }


class MeetingConsoleBridge(QObject):
    """Bound to QML as ``meetingConsole``."""

    stateChanged = Signal()
    participantsChanged = Signal()
    settingsRequested = Signal()

    def __init__(self, service, provider_switch, settings_store,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._switch = provider_switch
        self._settings = settings_store
        self._participants: list[dict[str, Any]] = []
        self._hands: list[dict[str, Any]] = []
        self._failed = False
        service.state_changed.connect(self._on_state)
        service.connection_changed.connect(self.stateChanged)
        service.busy_changed.connect(self.stateChanged)
        service.operation_finished.connect(self.stateChanged)
        service.attendance_changed.connect(self.stateChanged)
        service.participants_changed.connect(self._on_participants)
        service.hands_changed.connect(self._on_hands)
        service.error.connect(lambda _message: self._set_failed(True))
        provider_switch.active_changed.connect(self.stateChanged)

    # -- state ----------------------------------------------------------------------------
    def _get_hosted(self) -> bool:
        return self._switch.active == MeetingProviderChoice.SOLIN

    def _get_available(self) -> bool:
        return bool(self._service.is_available)

    def _get_state(self) -> str:
        return self._service.state.value

    def _get_open(self) -> bool:
        return self._service.state is MeetingState.OPEN

    def _get_connected(self) -> bool:
        return bool(self._service.is_connected)

    def _get_locked(self) -> bool:
        return bool(self._service.locked)

    def _get_busy(self) -> bool:
        return bool(self._service.is_busy)

    def _get_attendance(self) -> int:
        return int(self._service.attendance())

    def _get_failed(self) -> bool:
        return self._failed or self._service.state is MeetingState.FAILED

    def _get_participants(self) -> list:
        return self._participants

    def _get_hands(self) -> list:
        return self._hands

    def _get_attendee_address(self) -> str:
        # The address only: the full link carries the encryption key and is copied, not shown.
        return str(self._service.join_url).split("#", 1)[0]

    def _get_lobby_count(self) -> int:
        return sum(1 for row in self._participants if row["status"] == ParticipantStatus.LOBBY)

    hosted = Property(bool, _get_hosted, notify=stateChanged)
    available = Property(bool, _get_available, notify=stateChanged)
    state = Property(str, _get_state, notify=stateChanged)
    open = Property(bool, _get_open, notify=stateChanged)
    connected = Property(bool, _get_connected, notify=stateChanged)
    locked = Property(bool, _get_locked, notify=stateChanged)
    busy = Property(bool, _get_busy, notify=stateChanged)
    attendance = Property(int, _get_attendance, notify=stateChanged)
    failed = Property(bool, _get_failed, notify=stateChanged)
    participants = Property("QVariantList", _get_participants, notify=participantsChanged)
    hands = Property("QVariantList", _get_hands, notify=participantsChanged)
    lobbyCount = Property(int, _get_lobby_count, notify=participantsChanged)
    attendeeAddress = Property(str, _get_attendee_address, notify=stateChanged)

    # -- commands ---------------------------------------------------------------------------
    @Slot()
    def startMeeting(self) -> None:
        if not self._get_hosted() or not self._get_available() or self._service.is_enabled:
            return
        self._set_failed(False)
        self._service.configure(self._settings.meeting_settings())
        self._service.start()

    @Slot()
    def stopMeeting(self) -> None:
        self._service.stop()
        self._set_failed(False)
        self._participants, self._hands = [], []
        self.participantsChanged.emit()

    @Slot()
    def lock(self) -> None:
        self._service.request_pre_meeting()

    @Slot()
    def unlock(self) -> None:
        self._service.request_post_meeting()

    @Slot(str)
    def admit(self, identity: str) -> None:
        self._service.admit(identity)

    @Slot()
    def admitAll(self) -> None:
        self._service.admit_all()

    @Slot(str)
    def toLobby(self, identity: str) -> None:
        self._service.to_lobby(identity)

    @Slot(str)
    def callOn(self, identity: str) -> None:
        self._service.call_on(identity)

    @Slot(str)
    def finish(self, identity: str) -> None:
        self._service.finish(identity)

    @Slot(str)
    def lowerHand(self, identity: str) -> None:
        self._service.lower_hand(identity)

    @Slot(str, str)
    def rename(self, identity: str, name: str) -> None:
        self._service.rename(identity, name)

    @Slot(str)
    def remove(self, identity: str) -> None:
        self._service.remove(identity)

    @Slot()
    def copyJoinLink(self) -> None:
        self._copy(self._service.join_url)

    @Slot()
    def copyStageLink(self) -> None:
        self._copy(self._service.stage_url)

    def _copy(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if text and clipboard is not None:
            clipboard.setText(text)

    @Slot()
    def openSettings(self) -> None:
        self.settingsRequested.emit()

    # -- updates ----------------------------------------------------------------------------
    def _on_state(self, _state: str) -> None:
        self.stateChanged.emit()

    def _on_participants(self, participants) -> None:
        positions = {row["identity"]: row["handPosition"] for row in self._hands}
        self._participants = [participant_row(p, positions.get(p.identity, 0)) for p in participants]
        self.participantsChanged.emit()

    def _on_hands(self, hands) -> None:
        self._hands = [participant_row(p, index + 1) for index, p in enumerate(hands)]
        positions = {row["identity"]: row["handPosition"] for row in self._hands}
        for row in self._participants:
            row["handPosition"] = positions.get(row["identity"], 0)
        self.participantsChanged.emit()

    def _set_failed(self, failed: bool) -> None:
        if failed != self._failed:
            self._failed = failed
            self.stateChanged.emit()
