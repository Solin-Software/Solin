"""QML presentation bridge and host for profile onboarding."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import (
    QObject,
    Property,
    QCoreApplication,
    QT_TRANSLATE_NOOP,
    Signal,
    Slot,
    Qt,
    QUrl,
)
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtQuickWidgets import QQuickWidget

from solin.core.i18n.meeting_schedule import meeting_weekday_names
from solin.core.integrations.automation.obs import OBSConnectionState
from solin.core.integrations.automation.screen_share import (
    macos_accessibility_trusted,
)
from solin.core.jw.congregation_lookup import (
    MINIMUM_QUERY_LENGTH,
    RATE_LIMITED,
    SEARCH_DEBOUNCE_MS,
)
from solin.core.meetings.schedule import MeetingSchedule
from solin.core.onboarding.application import (
    OBSOnboardingConfiguration,
    OnboardingService,
    ProfileOnboardingCommand,
    ZoomShareOnboardingConfiguration,
)
from solin.styles.icons import (
    ICON_ARROW_LEFT,
    ICON_AUTO_DOWNLOAD,
    ICON_BOOK,
    ICON_CALENDAR,
    ICON_CHEVRON_DOWN,
    ICON_CLOSE,
    ICON_CROSSHAIR,
    ICON_MANUAL_DOWNLOAD,
    ICON_MEDIA_LANGUAGE,
    ICON_NAV_BROWSER,
    ICON_OBS,
    ICON_PLUG,
    ICON_SHARE_SCREEN,
    ICON_ZOOM,
)
from solin.styles.theme import PALETTE
from solin.ui.obs_status_text import translated_obs_status_text
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.svg_icons import SvgIconProvider

log = logging.getLogger(__name__)


_PAGE_PROFILE = "profile"
_PAGE_PREFERENCES = "preferences"
_PAGE_INTEGRATIONS = "integrations"
_PAGE_OBS = "obs"
_PAGE_ZOOM = "zoom"
_PAGE_REVIEW = "review"
_CONGREGATION_STATUS_SOURCES = {
    "searching": QT_TRANSLATE_NOOP("OnboardingView", "Searching jw.org…"),
    "resolving": QT_TRANSLATE_NOOP(
        "OnboardingView",
        "Reading the meeting times…",
    ),
    "empty": QT_TRANSLATE_NOOP(
        "OnboardingView",
        "No congregation found with that name.",
    ),
    "unpublished": QT_TRANSLATE_NOOP(
        "OnboardingView",
        "jw.org does not publish meeting times for this congregation.",
    ),
    "rate_limited": QT_TRANSLATE_NOOP(
        "OnboardingView",
        "Too many searches in a row. Wait a moment and type again.",
    ),
    "error": QT_TRANSLATE_NOOP(
        "OnboardingView",
        "Could not reach jw.org. Check the connection and try again.",
    ),
}
_OBS_SETUP_GUIDE_URL = "https://solinav.vercel.app/guide/#obs-studio-integration"
_ZOOM_SETUP_GUIDE_URL = "https://solinav.vercel.app/guide/#zoom-meetings-integration"


class OnboardingBridge(QObject):
    """Own the onboarding draft and expose a reactive, store-free QML API."""

    stateChanged = Signal()
    languagesChanged = Signal()
    completed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        *,
        language_manager: Any,
        onboarding_service: OnboardingService,
        obs_probe: Any,
        congregation_lookup: Any,
        target_picker_factory: Callable[..., Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._language_manager = language_manager
        self._onboarding = onboarding_service
        self._obs_probe = obs_probe
        self._congregation_lookup = congregation_lookup
        self._target_picker_factory = target_picker_factory
        self._target_picker: Any | None = None
        self._target_picker_token = 0
        self._history: list[str] = []
        self._session_revision = 0
        self._original_language = "en"
        self._media_language_touched = False
        self._obs_status_state = OBSConnectionState.DISCONNECTED
        self._obs_status_message = ""
        self._meeting_schedule: MeetingSchedule | None = None
        self._congregation_status_kind = "idle"
        self._state: dict[str, Any] = {}
        self._reset_state("Profile 1", allow_cancel=False)

        self._obs_probe.state_changed.connect(self._on_obs_state_changed)
        self._obs_probe.scenes_updated.connect(self._on_obs_scenes_updated)
        self._congregation_lookup.suggestions_ready.connect(
            self._on_congregation_suggestions
        )
        self._congregation_lookup.schedule_ready.connect(
            self._on_congregation_schedule
        )
        self._congregation_lookup.failed.connect(self._on_congregation_failed)
        if self._language_manager is not None:
            self._language_manager.language_changed.connect(
                self._on_language_changed
            )
            media_service = self._language_manager.jw_lang_service
            media_service.languages_ready.connect(self._on_media_languages_ready)
            media_service.fetch_if_needed()

    @Property(int, constant=True)
    def searchDebounceMs(self) -> int:  # noqa: N802 - QML API
        return SEARCH_DEBOUNCE_MS

    @Property("QVariantMap", notify=stateChanged)
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    @Property("QVariantList", notify=languagesChanged)
    def interfaceLanguages(self) -> list[dict[str, str]]:  # noqa: N802 - QML API
        if self._language_manager is None:
            items = [("en", "English"), ("pt_BR", "Português (Brasil)")]
        else:
            items = sorted(
                self._language_manager.available_languages(),
                key=lambda item: item[1].casefold(),
            )
        return [{"code": code, "name": name} for code, name in items]

    @Property("QVariantList", notify=languagesChanged)
    def mediaLanguages(self) -> list[dict[str, Any]]:  # noqa: N802 - QML API
        service = (
            self._language_manager.jw_lang_service
            if self._language_manager is not None
            else None
        )
        if service is not None and service.has_data:
            languages = sorted(
                service.languages,
                key=lambda item: (
                    item.get("vernacular")
                    or item.get("name")
                    or item.get("code", "")
                ).casefold(),
            )
            return [
                {
                    "code": item.get("code", ""),
                    "name": item.get("vernacular") or item.get("name") or "",
                    "secondary": item.get("name") or item.get("code") or "",
                    "rtl": bool(item.get("isRTL")),
                }
                for item in languages
                if item.get("code")
            ]
        return [
            {"code": "E", "name": "English", "secondary": "E", "rtl": False},
            {"code": "T", "name": "Português", "secondary": "T", "rtl": False},
            {"code": "S", "name": "Español", "secondary": "S", "rtl": False},
            {"code": "I", "name": "Italiano", "secondary": "I", "rtl": False},
        ]

    def start(self, profile_name: str, *, allow_cancel: bool) -> None:
        self._original_language = (
            self._language_manager.current_code
            if self._language_manager is not None
            else "en"
        )
        self._history.clear()
        self._session_revision += 1
        self._media_language_touched = False
        self._meeting_schedule = None
        self._congregation_status_kind = "idle"
        self._obs_probe.stop()
        self._reset_state(profile_name, allow_cancel=allow_cancel)
        self.stateChanged.emit()

    def shutdown(self) -> None:
        self._obs_probe.stop()
        self._close_target_picker()

    @Slot(str, "QVariant")
    def updateField(self, name: str, value: Any) -> None:  # noqa: N802 - QML API
        if name not in self._state:
            return
        normalized: Any = value
        if name in {
            "downloadMeetingMedia",
            "obsSelected",
            "zoomSelected",
            "obsAutomatic",
        }:
            normalized = bool(value)
        elif name in {"profileName", "obsPort", "obsPassword"}:
            normalized = str(value)
        self._state[name] = normalized
        self._state["errorText"] = ""
        if name == "obsSelected" and not normalized:
            self._obs_probe.stop()
            self._state["obsConnected"] = False
            self._state["obsState"] = "idle"
            self._obs_status_state = OBSConnectionState.DISCONNECTED
            self._obs_status_message = ""
            self._state["obsStatusText"] = self._onboarding_obs_status_text()
        self.stateChanged.emit()

    @Slot(str, str)
    def chooseLanguage(self, kind: str, code: str) -> None:  # noqa: N802 - QML API
        if kind == "interface":
            name = self._interface_language_name(code)
            if not name:
                return
            self._state["interfaceCode"] = code
            self._state["interfaceName"] = name
            if not self._media_language_touched:
                media_code = self._interface_api_code(code)
                if media_code:
                    self._state["mediaCode"] = media_code
                    self._state["mediaName"] = self._media_language_name(media_code)
            if self._language_manager is not None:
                self._language_manager.preview_language(code)
        elif kind == "media":
            name = self._media_language_name(code)
            if not name:
                return
            self._media_language_touched = True
            self._state["mediaCode"] = code
            self._state["mediaName"] = name
        else:
            return
        self._state["errorText"] = ""
        self.stateChanged.emit()

    @Slot()
    def advance(self) -> None:
        if self._state["busy"]:
            return
        current = self._state["currentPage"]
        error = self._validate_page(current)
        if error:
            self._state["errorText"] = error
            self.stateChanged.emit()
            return
        if current == _PAGE_REVIEW:
            self._complete()
            return
        self._confirm_current_integration(current)
        next_page = self._next_page(current)
        self._history.append(current)
        self._navigate(next_page, direction=1)

    @Slot()
    def back(self) -> None:
        if self._state["busy"] or not self._history:
            return
        self._navigate(self._history.pop(), direction=-1)

    @Slot()
    def cancel(self) -> None:
        if not self._state["allowCancel"] or self._state["busy"]:
            return
        self.shutdown()
        self._restore_original_language()
        self.cancelled.emit()

    @Slot()
    def skipCurrentIntegration(self) -> None:  # noqa: N802 - QML API
        current = self._state["currentPage"]
        if current == _PAGE_OBS:
            self._state["obsSelected"] = False
            self._obs_probe.stop()
        elif current == _PAGE_ZOOM:
            self._state["zoomSelected"] = False
        else:
            return
        self._state["errorText"] = ""
        next_page = self._next_page(current)
        self._history.append(current)
        self._navigate(next_page, direction=1)

    @Slot()
    def testObsConnection(self) -> None:  # noqa: N802 - QML API
        if self._state["busy"]:
            return
        try:
            port = int(str(self._state["obsPort"]).strip() or "4455")
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            self._state["errorText"] = QCoreApplication.translate(
                "OnboardingView",
                "Enter a port between 1 and 65535.",
            )
            self.stateChanged.emit()
            return
        self._obs_status_state = OBSConnectionState.CONNECTING
        self._obs_status_message = ""
        self._state.update(
            {
                "errorText": "",
                "obsState": "connecting",
                "obsStatusText": self._onboarding_obs_status_text(),
                "obsConnected": False,
            }
        )
        self.stateChanged.emit()
        self._obs_probe.connect_to(port, str(self._state["obsPassword"]))

    @Slot(int, int, result=bool)
    def captureHotkey(self, key: int, modifiers: int) -> bool:  # noqa: N802 - QML API
        ignored = {
            int(Qt.Key.Key_Control),
            int(Qt.Key.Key_Shift),
            int(Qt.Key.Key_Alt),
            int(Qt.Key.Key_Meta),
            int(Qt.Key.Key_unknown),
        }
        if key in ignored:
            return False
        sequence = QKeySequence(modifiers | key).toString(
            QKeySequence.SequenceFormat.PortableText
        )
        if not sequence:
            return False
        self._state["zoomHotkey"] = sequence
        self._state["errorText"] = ""
        self.stateChanged.emit()
        return True

    @Slot()
    def configureZoomTarget(self) -> None:  # noqa: N802 - QML API
        if not self._state["zoomAvailable"]:
            self._state["errorText"] = self._state["zoomUnavailableReason"]
            self.stateChanged.emit()
            return
        if self._target_picker_factory is None:
            self._state["errorText"] = QCoreApplication.translate(
                "OnboardingView",
                "The share target picker is unavailable.",
            )
            self.stateChanged.emit()
            return
        self._close_target_picker()
        self._target_picker_token += 1
        token = self._target_picker_token
        current_x = -1
        current_y = -1
        if self._state["zoomTargetConfigured"]:
            current_x = int(self._state["zoomClickX"])
            current_y = int(self._state["zoomClickY"])
        picker = self._target_picker_factory(
            current_x=current_x,
            current_y=current_y,
            parent=None,
        )
        self._target_picker = picker
        picker.position_picked.connect(self._on_target_picked)
        if hasattr(picker, "destroyed"):
            picker.destroyed.connect(
                lambda _obj=None, token=token: self._on_target_picker_destroyed(token)
            )
        if hasattr(picker, "cancelled"):
            picker.cancelled.connect(self._on_target_picker_cancelled)
        picker.show_overlay()

    @Slot()
    def openAccessibilitySettings(self) -> None:  # noqa: N802 - QML API
        if sys.platform != "darwin":
            return
        QDesktopServices.openUrl(
            QUrl(
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility"
            )
        )

    @Slot()
    def openObsSetupGuide(self) -> None:  # noqa: N802 - QML API
        QDesktopServices.openUrl(QUrl(_OBS_SETUP_GUIDE_URL))

    @Slot()
    def openZoomSetupGuide(self) -> None:  # noqa: N802 - QML API
        QDesktopServices.openUrl(QUrl(_ZOOM_SETUP_GUIDE_URL))

    @Slot(str)
    def searchCongregation(self, text: str) -> None:  # noqa: N802 - QML API
        self._state["congregationQuery"] = text
        searching = len(text.strip()) >= MINIMUM_QUERY_LENGTH
        self._congregation_status_kind = "searching" if searching else "idle"
        self._publish_congregation_state()
        self._congregation_lookup.search(text)

    @Slot(str, str)
    def chooseCongregation(self, guid: str, name: str) -> None:  # noqa: N802 - QML API
        if not guid:
            return
        self._meeting_schedule = None
        self._congregation_status_kind = "resolving"
        self._state.update(
            {
                "congregationName": name,
                "congregationQuery": name,
                "congregationScheduleText": "",
                "congregationSuggestions": [],
            }
        )
        self._publish_congregation_state()
        self._congregation_lookup.fetch_schedule(guid)

    @Slot()
    def clearCongregation(self) -> None:  # noqa: N802 - QML API
        self._meeting_schedule = None
        self._congregation_status_kind = "idle"
        self._state.update(
            {
                "congregationName": "",
                "congregationQuery": "",
                "congregationScheduleText": "",
                "congregationSuggestions": [],
            }
        )
        self._publish_congregation_state()

    def _reset_state(self, profile_name: str, *, allow_cancel: bool) -> None:
        interface_code = (
            self._language_manager.current_code
            if self._language_manager is not None
            else "en"
        )
        media_code = self._interface_api_code(interface_code) or "E"
        zoom_available, zoom_reason = self._zoom_capability()
        self._obs_status_state = OBSConnectionState.DISCONNECTED
        self._obs_status_message = ""
        self._state = {
            "sessionRevision": self._session_revision,
            "currentPage": _PAGE_PROFILE,
            "progressStage": 0,
            "direction": 1,
            "allowCancel": allow_cancel,
            "profileName": profile_name,
            "interfaceCode": interface_code,
            "interfaceName": self._interface_language_name(interface_code),
            "mediaCode": media_code,
            "mediaName": self._media_language_name(media_code),
            "downloadMeetingMedia": False,
            "congregationQuery": "",
            "congregationName": "",
            "congregationScheduleText": "",
            "congregationStatusText": self._congregation_status_text(),
            "congregationSuggestions": [],
            "obsSelected": False,
            "zoomSelected": False,
            "obsPort": "4455",
            "obsPassword": "",
            "obsState": "idle",
            "obsStatusText": self._onboarding_obs_status_text(),
            "obsConnected": False,
            "obsScenes": [],
            "obsAutomatic": False,
            "obsDefaultScene": "",
            "obsMediaScene": "",
            "zoomAvailable": zoom_available,
            "zoomUnavailableReason": zoom_reason,
            "zoomAccessibilityRequired": (
                sys.platform == "darwin" and not zoom_available
            ),
            "zoomHotkey": "",
            "zoomTargetConfigured": False,
            "zoomClickX": -1,
            "zoomClickY": -1,
            "busy": False,
            "errorText": "",
        }

    def _validate_page(self, page: str) -> str:
        if page == _PAGE_PROFILE and not str(self._state["profileName"]).strip():
            return QCoreApplication.translate("OnboardingView", "Enter a profile name.")
        if page == _PAGE_OBS:
            if not self._state["obsConnected"]:
                return QCoreApplication.translate(
                    "OnboardingView",
                    "Connect to OBS or choose Set up later.",
                )
            if self._state["obsAutomatic"]:
                default_scene = str(self._state["obsDefaultScene"])
                media_scene = str(self._state["obsMediaScene"])
                if not default_scene or not media_scene:
                    return QCoreApplication.translate(
                        "OnboardingView",
                        "Choose both OBS scenes.",
                    )
                if default_scene == media_scene:
                    return QCoreApplication.translate(
                        "OnboardingView",
                        "Choose two different OBS scenes.",
                    )
        if page == _PAGE_ZOOM:
            if not self._state["zoomAvailable"]:
                return self._state["zoomUnavailableReason"]
            if not str(self._state["zoomHotkey"]).strip():
                return QCoreApplication.translate(
                    "OnboardingView",
                    "Record the Zoom share shortcut.",
                )
            if not self._state["zoomTargetConfigured"]:
                return QCoreApplication.translate(
                    "OnboardingView",
                    "Choose the target in Zoom's share dialog.",
                )
        return ""

    def _confirm_current_integration(self, page: str) -> None:
        if page == _PAGE_OBS:
            self._state["obsSelected"] = True
        elif page == _PAGE_ZOOM:
            self._state["zoomSelected"] = True

    def _next_page(self, current: str) -> str:
        if current == _PAGE_PROFILE:
            return _PAGE_PREFERENCES
        if current == _PAGE_PREFERENCES:
            return _PAGE_INTEGRATIONS
        if current == _PAGE_INTEGRATIONS:
            if self._state["obsSelected"]:
                return _PAGE_OBS
            if self._state["zoomSelected"]:
                return _PAGE_ZOOM
            return _PAGE_REVIEW
        if current == _PAGE_OBS:
            return _PAGE_ZOOM if self._state["zoomSelected"] else _PAGE_REVIEW
        if current == _PAGE_ZOOM:
            return _PAGE_REVIEW
        return _PAGE_REVIEW

    def _navigate(self, page: str, *, direction: int) -> None:
        self._state["currentPage"] = page
        self._state["progressStage"] = {
            _PAGE_PROFILE: 0,
            _PAGE_PREFERENCES: 1,
            _PAGE_INTEGRATIONS: 2,
            _PAGE_OBS: 2,
            _PAGE_ZOOM: 2,
            _PAGE_REVIEW: 3,
        }[page]
        self._state["direction"] = direction
        self._state["errorText"] = ""
        self.stateChanged.emit()

    def _complete(self) -> None:
        self._state["busy"] = True
        self._state["errorText"] = ""
        self.stateChanged.emit()
        try:
            obs_enabled = bool(self._state["obsSelected"])
            zoom_enabled = bool(self._state["zoomSelected"])
            obs_port = int(str(self._state["obsPort"]) or "4455") if obs_enabled else 4455
            profile = self._onboarding.complete(
                ProfileOnboardingCommand(
                    name=str(self._state["profileName"]),
                    interface_language=str(self._state["interfaceCode"]),
                    media_language=str(self._state["mediaCode"]),
                    download_meeting_media=bool(
                        self._state["downloadMeetingMedia"]
                    ),
                    meeting_schedule=self._meeting_schedule,
                    obs=OBSOnboardingConfiguration(
                        enabled=obs_enabled,
                        port=obs_port,
                        password=str(self._state["obsPassword"]),
                        automatic_scene_switching=(
                            obs_enabled and bool(self._state["obsAutomatic"])
                        ),
                        default_scene=str(self._state["obsDefaultScene"]),
                        media_scene=str(self._state["obsMediaScene"]),
                    ),
                    zoom_share=ZoomShareOnboardingConfiguration(
                        enabled=zoom_enabled,
                        hotkey=str(self._state["zoomHotkey"]),
                        click_x=int(self._state["zoomClickX"]),
                        click_y=int(self._state["zoomClickY"]),
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - UI transaction boundary
            log.exception("Could not complete onboarding")
            self._state["busy"] = False
            self._state["errorText"] = str(exc) or QCoreApplication.translate(
                "OnboardingView",
                "Could not create the profile.",
            )
            self.stateChanged.emit()
            return
        self.shutdown()
        self.completed.emit(profile.id)

    def _on_obs_state_changed(
        self,
        state: OBSConnectionState,
        message: str,
    ) -> None:
        key = {
            OBSConnectionState.DISCONNECTED: "idle",
            OBSConnectionState.CONNECTING: "connecting",
            OBSConnectionState.CONNECTED: "connected",
            OBSConnectionState.ERROR: "error",
        }.get(state, "idle")
        self._obs_status_state = state
        self._obs_status_message = message or ""
        text = self._onboarding_obs_status_text()
        self._state["obsState"] = key
        self._state["obsStatusText"] = text
        self._state["obsConnected"] = state is OBSConnectionState.CONNECTED
        if state is OBSConnectionState.ERROR:
            self._state["errorText"] = text
        self.stateChanged.emit()

    def _on_obs_scenes_updated(self, scenes: list[str]) -> None:
        normalized = [str(scene) for scene in scenes if str(scene).strip()]
        self._state["obsScenes"] = normalized
        for field in ("obsDefaultScene", "obsMediaScene"):
            if self._state[field] not in normalized:
                self._state[field] = ""
        self.stateChanged.emit()

    def _on_target_picked(self, x: int, y: int) -> None:
        self._state["zoomClickX"] = int(x)
        self._state["zoomClickY"] = int(y)
        self._state["zoomTargetConfigured"] = True
        self._state["errorText"] = ""
        self.stateChanged.emit()

    def _on_target_picker_cancelled(self) -> None:
        self._target_picker = None

    def _on_target_picker_destroyed(self, token: int) -> None:
        if token == self._target_picker_token:
            self._target_picker = None

    def _close_target_picker(self) -> None:
        picker, self._target_picker = self._target_picker, None
        self._target_picker_token += 1
        if picker is not None:
            picker.close()

    def _on_congregation_suggestions(self, matches: list[Any]) -> None:
        self._congregation_status_kind = "idle" if matches else "empty"
        self._state["congregationSuggestions"] = [
            {
                "guid": match.guid,
                "name": match.name,
                "formattedName": match.formatted_name,
            }
            for match in matches
        ]
        self._publish_congregation_state()

    def _on_congregation_schedule(self, schedule: MeetingSchedule | None) -> None:
        self._meeting_schedule = schedule
        self._congregation_status_kind = "idle" if schedule else "unpublished"
        self._state["congregationScheduleText"] = self._congregation_schedule_text()
        self._publish_congregation_state()

    def _on_congregation_failed(self, reason: str) -> None:
        self._congregation_status_kind = (
            "rate_limited" if reason == RATE_LIMITED else "error"
        )
        self._state["congregationSuggestions"] = []
        self._publish_congregation_state()

    def _publish_congregation_state(self) -> None:
        """Report lookup progress in the sheet instead of the blocking banner."""

        self._state["congregationStatusText"] = self._congregation_status_text()
        self._state["errorText"] = ""
        self.stateChanged.emit()

    def _congregation_status_text(self) -> str:
        """Report progress and failures only; the idle sheet needs no caption."""

        source = _CONGREGATION_STATUS_SOURCES.get(self._congregation_status_kind)
        if source is None:
            return ""
        return QCoreApplication.translate("OnboardingView", source)

    def _congregation_schedule_text(self) -> str:
        if self._meeting_schedule is None:
            return ""
        weekdays = meeting_weekday_names()
        return "  ·  ".join(
            f"{weekdays[slot.weekday]} {slot.time_text}"
            for slot in self._meeting_schedule.slots
            if slot.is_configured
        )

    def _on_language_changed(self, _code: str) -> None:
        zoom_available, zoom_reason = self._zoom_capability()
        self._state["zoomAvailable"] = zoom_available
        self._state["zoomUnavailableReason"] = zoom_reason
        self._state["obsStatusText"] = self._onboarding_obs_status_text()
        self._state["congregationScheduleText"] = self._congregation_schedule_text()
        self._state["congregationStatusText"] = self._congregation_status_text()
        if self._obs_status_state is OBSConnectionState.ERROR:
            self._state["errorText"] = self._state["obsStatusText"]
        self.stateChanged.emit()

    def _on_media_languages_ready(self, _languages: list[Any]) -> None:
        self._state["mediaName"] = self._media_language_name(
            str(self._state["mediaCode"])
        )
        self.languagesChanged.emit()
        self.stateChanged.emit()

    def _restore_original_language(self) -> None:
        if self._language_manager is not None:
            self._language_manager.preview_language(self._original_language)

    def _interface_language_name(self, code: str) -> str:
        for item in self.interfaceLanguages:
            if item["code"] == code:
                return item["name"]
        return code

    def _interface_api_code(self, code: str) -> str:
        if self._language_manager is not None:
            return self._language_manager.api_code_for_language(code)
        return "T" if code == "pt_BR" else "E"

    def _media_language_name(self, code: str) -> str:
        for item in self.mediaLanguages:
            if item["code"] == code:
                return str(item["name"])
        return code

    @staticmethod
    def _zoom_capability() -> tuple[bool, str]:
        if sys.platform == "darwin":
            if macos_accessibility_trusted() is not True:
                return False, QCoreApplication.translate(
                    "OnboardingView",
                    "Allow Solin in macOS Accessibility settings.",
                )
            return True, ""
        if sys.platform.startswith("linux"):
            if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
                return False, QCoreApplication.translate(
                    "OnboardingView",
                    "Automatic sharing is unavailable on Wayland.",
                )
            if shutil.which("xdotool") is None:
                return False, QCoreApplication.translate(
                    "OnboardingView",
                    "Install xdotool to use automatic sharing.",
                )
        return True, ""

    def _onboarding_obs_status_text(self) -> str:
        return translated_obs_status_text(
            self._obs_status_state,
            self._obs_status_message,
            disconnected_source="Not connected",
            include_error_prefix=False,
        )


class OnboardingQmlHost(QQuickWidget):
    """Compose the QML onboarding scene and its Python bridge."""

    completed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        *,
        language_manager: Any,
        onboarding_service: OnboardingService,
        obs_probe: Any,
        congregation_lookup: Any,
        target_picker_factory: Callable[..., Any] | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.bridge = OnboardingBridge(
            language_manager=language_manager,
            onboarding_service=onboarding_service,
            obs_probe=obs_probe,
            congregation_lookup=congregation_lookup,
            target_picker_factory=target_picker_factory,
            parent=self,
        )
        self.bridge.completed.connect(self.completed.emit)
        self.bridge.cancelled.connect(self.cancelled.emit)

        icons = {
            "arrow_left": ICON_ARROW_LEFT,
            "auto_download": ICON_AUTO_DOWNLOAD,
            "book": ICON_BOOK,
            "calendar": ICON_CALENDAR,
            "chevron_down": ICON_CHEVRON_DOWN,
            "close": ICON_CLOSE,
            "crosshair": ICON_CROSSHAIR,
            "interface": ICON_NAV_BROWSER,
            "manual_download": ICON_MANUAL_DOWNLOAD,
            "media_language": ICON_MEDIA_LANGUAGE,
            "obs": ICON_OBS,
            "plug": ICON_PLUG,
            "share": ICON_SHARE_SCREEN,
            "zoom": ICON_ZOOM,
        }
        configure_qml_host(
            self,
            type_name="OnboardingView",
            clear_color=PALETTE.bg0,
            context_properties={"onboardingBridge": self.bridge},
            image_providers={
                "onboardingicons": SvgIconProvider(
                    icons,
                    default_icon="interface",
                )
            },
            mouse_tracking=True,
        )
        if language_manager is not None:
            language_manager.language_changed.connect(
                lambda _code: self.engine().retranslate()
            )

    def start(self, profile_name: str, *, allow_cancel: bool) -> None:
        self.bridge.start(profile_name, allow_cancel=allow_cancel)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def shutdown(self) -> None:
        self.bridge.shutdown()


__all__ = ["OnboardingBridge", "OnboardingQmlHost"]
