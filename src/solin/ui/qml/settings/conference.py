"""Settings domain for who hosts the meeting (Zoom or this computer) and how."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication, QObject

from solin.core.conference.settings import MeetingProviderChoice, MeetingQuality

from .domain import SettingsDomain


def _tr(text: str) -> str:
    return QCoreApplication.translate("SettingsWidget", text)


class ConferenceSettings(SettingsDomain):
    """Exposed to QML as ``settingsConference``."""

    def __init__(self, store, provider_switch, *, hosting_available: bool,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._switch = provider_switch
        self._hosting_available = hosting_available
        self.refresh_language()

    def refresh_language(self) -> None:
        provider = self._store.provider()
        self.publish(
            meetingProvider=provider.value,
            meetingProviderOptions=[
                {"value": MeetingProviderChoice.ZOOM.value, "label": _tr("Zoom")},
                {"value": MeetingProviderChoice.SOLIN.value,
                 "label": _tr("This computer (Solin)")},
            ],
            hostedSelected=provider is MeetingProviderChoice.SOLIN,
            hostingAvailable=self._hosting_available,
            meetingHallName=self._store.hall_name(),
            meetingQuality=self._store.quality().value,
            meetingQualityOptions=[
                {"value": MeetingQuality.SD.value, "label": _tr("Standard (360p)")},
                {"value": MeetingQuality.HD.value, "label": _tr("High (720p)")},
                {"value": MeetingQuality.AUDIO_ONLY.value, "label": _tr("Audio only")},
            ],
            meetingE2ee=self._store.e2ee(),
            meetingAttendeeVideo=self._store.attendee_video(),
        )

    def refresh_runtime(self) -> None:
        self.refresh_language()

    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - QML API
        if key == "meetingProvider":
            choice = MeetingProviderChoice(str(value))
            if choice is MeetingProviderChoice.SOLIN and not self._hosting_available:
                return
            self._store.set_provider(choice)
            self._switch.set_active(choice)
            self.publish(meetingProvider=choice.value,
                         hostedSelected=choice is MeetingProviderChoice.SOLIN)
        elif key == "meetingHallName":
            self._store.set_hall_name(str(value))
            self.publish(meetingHallName=self._store.hall_name())
        elif key == "meetingQuality":
            self._store.set_quality(MeetingQuality(str(value)))
            self.publish(meetingQuality=self._store.quality().value)
        elif key == "meetingE2ee":
            self._store.set_e2ee(bool(value))
            self.publish(meetingE2ee=self._store.e2ee())
        elif key == "meetingAttendeeVideo":
            self._store.set_attendee_video(bool(value))
            self.publish(meetingAttendeeVideo=self._store.attendee_video())
        else:
            super().setValue(key, value)

    def cleanup(self) -> None:
        """Nothing to release; present for the settings host's domain contract."""
