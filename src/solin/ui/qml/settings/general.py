"""Settings presentation state for appearance, media, meetings and projection."""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Property, QCoreApplication, QT_TRANSLATE_NOOP, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from solin.core.i18n.meeting_schedule import (
    meeting_not_configured_label,
    meeting_weekday_names,
)
from solin.core.jw.language_context import jw_media_language_context
from solin.core.jw.congregation_lookup import (
    MINIMUM_QUERY_LENGTH,
    RATE_LIMITED,
    SEARCH_DEBOUNCE_MS,
)
from solin.core.meetings.schedule import (
    DEFAULT_MIDWEEK_TIME,
    DEFAULT_WEEKEND_TIME,
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    parse_time_text,
)
from solin.styles.theme import available_themes, current_theme, normalize_theme_id

from .domain import SettingsDomain


_CONGREGATION_STATUS_SOURCES = {
    "searching": QT_TRANSLATE_NOOP("SettingsWidget", "Searching jw.org…"),
    "resolving": QT_TRANSLATE_NOOP("SettingsWidget", "Reading the meeting times…"),
    "empty": QT_TRANSLATE_NOOP("SettingsWidget", "No congregation found with that name."),
    "unpublished": QT_TRANSLATE_NOOP(
        "SettingsWidget",
        "jw.org does not publish meeting times for this congregation.",
    ),
    "rate_limited": QT_TRANSLATE_NOOP(
        "SettingsWidget",
        "Too many searches in a row. Wait a moment and type again.",
    ),
    "error": QT_TRANSLATE_NOOP(
        "SettingsWidget",
        "Could not reach jw.org. Check the connection and try again.",
    ),
}
_CONGREGATION_HINT = QT_TRANSLATE_NOOP(
    "SettingsWidget",
    "Search your congregation to fill the days and times below.",
)


class GeneralSettings(SettingsDomain):
    """Keep service lifetime and edit drafts independent of loaded QML pages."""

    language_changed = Signal(str)
    yearly_text_changed = Signal(str, str, str)
    watched_folder_changed = Signal(str)
    background_song_toggled = Signal(bool)
    meetings_auto_download_toggled = Signal(bool)
    meeting_schedule_changed = Signal()
    theme_changed = Signal(str)

    def __init__(
        self, *, lang_manager, screen_manager, app_settings,
        media_settings, playback_protection, meeting_schedule_settings,
        watched_folder_settings, yeartext_settings, background_song_settings,
        yeartext_service_factory, congregation_lookup_factory,
        parent=None, dialog_parent=None,
    ):
        super().__init__(parent)
        self.lang = lang_manager
        self._screens = screen_manager
        self._app_settings = app_settings
        self._media_settings = media_settings
        self._playback_protection = playback_protection
        self._meeting_schedule_settings = meeting_schedule_settings
        self._watched_folder_settings = watched_folder_settings
        self._yeartext_settings = yeartext_settings
        self._background_song_settings = background_song_settings
        self._dialog_parent = dialog_parent
        self._closed = False
        self._started = False
        self._expected_yeartext: set[tuple[str, int]] = set()
        self._fallback_requested = False
        self._yeartext_result = None
        self._yt_service = yeartext_service_factory(self)
        self._congregation_lookup = congregation_lookup_factory(self)
        self._congregation_status_kind = "idle"
        self._congregation_schedule = None
        self._connections = []
        for signal, callback in (
            (self._yt_service.fetched, self._on_yeartext_fetched),
            (self._yt_service.fetch_failed, self._on_yeartext_failed),
            (self._yt_service.fetch_started, self._on_fetch_started),
            (screen_manager.screens_changed, self.refresh_runtime),
            (lang_manager.language_changed, self._on_language_switched),
            (lang_manager.jw_lang_service.media_language_changed, self._on_language_switched),
            (lang_manager.jw_lang_service.languages_ready, self._on_languages_ready),
            (lang_manager.jw_lang_service.fetch_started, self._on_languages_loading),
            (lang_manager.jw_lang_service.fetch_failed, self._on_languages_failed),
            (playback_protection.enabledChanged, self._sync_playback_protection),
            (self._congregation_lookup.suggestions_ready, self._on_congregation_suggestions),
            (self._congregation_lookup.schedule_ready, self._on_congregation_schedule),
            (self._congregation_lookup.failed, self._on_congregation_failed),
        ):
            signal.connect(callback)
            self._connections.append((signal, callback))
        quote, reference = yeartext_settings.text()
        self.publish(
            yeartextQuote=quote, yeartextReference=reference, yeartextDirty=False,
            yeartextStatus="idle", yeartextStatusText="", yeartextPreview="",
            yeartextError="", watchedFolder=watched_folder_settings.path(),
            autoDownloadOnPlay=media_settings.auto_download_on_play(),
            meetingsAutoDownload=media_settings.meetings_auto_download(),
            songAnnouncement=media_settings.sjjm_announce_mode(),
            startVideosPaused=media_settings.start_videos_paused(),
            playbackProtection=playback_protection.enabled,
            backgroundSongEnabled=background_song_settings.is_enabled(),
            mediaLanguagesError="",
            congregationQuery="", congregationName="", congregationSuggestions=[],
            congregationStatusKind="idle", congregationStatusText="",
        )
        self._refresh_schedule()
        self.refresh_language()
        self.refresh_runtime()

    def refresh_language(self) -> None:
        if self._closed:
            return
        svc = self.lang.jw_lang_service
        media_code = svc.media_api_code
        language = svc.get_language(media_code) if media_code else None
        interface_name = self.lang.meta.get("name", self.lang.current_code)
        media_options = [{
            "value": "",
            "label": QCoreApplication.translate("SettingsWidget", "(same as interface)"),
        }]
        media_options.extend(
            {"value": item["code"],
             "label": item.get("vernacular") or item.get("name") or item["code"],
             "description": item.get("name", "")}
            for item in sorted(svc.languages, key=lambda item: (
                item.get("vernacular") or item.get("name") or ""
            ).casefold()) if item.get("code")
        )
        self.publish(
            themeId=normalize_theme_id(self._app_settings.app_theme_id()),
            themeOptions=[{"value": theme.id, "label": (
                QCoreApplication.translate("SettingsWidget", "Dark")
                if theme.id == "dark" else
                QCoreApplication.translate("SettingsWidget", "Light")
                if theme.id == "light" else theme.display_name
            )} for theme in available_themes()],
            interfaceLanguage=self.lang.current_code,
            interfaceLanguageName=interface_name,
            interfaceLanguageOptions=[{"value": code, "label": name, "description": code}
                                      for code, name in self.lang.available_languages()],
            mediaLanguage=media_code,
            mediaLanguageName=(language.get("vernacular") or language.get("name") or media_code)
            if language else media_code or (
                f"{interface_name}  "
                f"{QCoreApplication.translate('SettingsWidget', '(same as interface)')}"
            ),
            mediaLanguageOptions=media_options,
            mediaLanguagesLoading=svc.is_loading,
            dayOptions=[{"value": -1, "label": meeting_not_configured_label()}] + [
                {"value": index, "label": label}
                for index, label in enumerate(meeting_weekday_names())
            ],
            backgroundSongDescription=self._background_song_description(),
        )
        self._refresh_yeartext_status()
        self._refresh_congregation_status()

    @Property(int, constant=True)
    def searchDebounceMs(self) -> int:  # noqa: N802 - QML API
        return SEARCH_DEBOUNCE_MS

    def refresh_runtime(self) -> None:
        if self._closed:
            return
        primary = self._screens.primary_screen()
        screens = ([primary] if primary is not None else []) + list(self._screens.secondary_screens())
        self.publish(screens=[{
            "name": screen.name(), "width": screen.geometry().width(),
            "height": screen.geometry().height(), "primary": screen == primary,
            "label": QCoreApplication.translate("SettingsWidget", "Primary Screen (control)")
            if screen == primary else QCoreApplication.translate(
                "SettingsWidget", "Secondary {n} (projection)"
            ).replace("{n}", str(index)),
        } for index, screen in enumerate(screens)])

    @Slot(str, "QVariant")
    def setValue(self, key: str, value) -> None:
        if self._closed:
            return
        try:
            self._set_value(key, value)
        except (OSError, ValueError, RuntimeError) as exc:
            self.fail(str(exc))

    def _set_value(self, key: str, value) -> None:
        setters = {
            "autoDownloadOnPlay": self._media_settings.set_auto_download_on_play,
            "meetingsAutoDownload": self._media_settings.set_meetings_auto_download,
            "songAnnouncement": self._media_settings.set_sjjm_announce_mode,
            "startVideosPaused": self._media_settings.set_start_videos_paused,
            "playbackProtection": self._playback_protection.set_enabled,
            "backgroundSongEnabled": self._background_song_settings.set_enabled,
        }
        if key in setters:
            enabled = bool(value)
            setters[key](enabled)
            self.publish(**{key: enabled})
            if key == "meetingsAutoDownload":
                self.meetings_auto_download_toggled.emit(enabled)
            elif key == "backgroundSongEnabled":
                self.background_song_toggled.emit(enabled)
        elif key == "themeId":
            if value not in {theme.id for theme in available_themes()}:
                return
            theme_id = normalize_theme_id(str(value))
            self._app_settings.set_app_theme_id(theme_id)
            self.publish(themeId=theme_id)
            if theme_id != current_theme().id:
                self.theme_changed.emit(theme_id)
        elif key == "interfaceLanguage":
            if value in dict(self.lang.available_languages()) and value != self.lang.current_code:
                self.lang.set_language(value)
                self.refresh_language()
                self.language_changed.emit(value)
        elif key == "mediaLanguage":
            if value in {option["value"] for option in self._state["mediaLanguageOptions"]}:
                self.lang.jw_lang_service.set_media_api_code(str(value))
                self.refresh_language()
        elif key in ("midweekDay", "weekendDay", "midweekTime", "weekendTime"):
            self._set_schedule(key, value)
        elif key in ("yeartextQuote", "yeartextReference"):
            self.publish(**{key: str(value), "yeartextDirty": True})

    @Slot(str)
    def invoke(self, action: str) -> None:
        if self._closed:
            return
        actions = {
            "chooseFolder": self._choose_folder,
            "clearFolder": self._clear_folder,
            "refreshYeartext": self._refresh_yeartext,
            "saveYeartext": self._save_yeartext,
            "cancelYeartext": self._cancel_yeartext,
            "refreshMediaLanguages": self.lang.jw_lang_service.force_refresh,
        }
        callback = actions.get(action)
        if callback is not None:
            try:
                callback()
            except (OSError, ValueError, RuntimeError) as exc:
                self.fail(str(exc))

    def _refresh_schedule(self) -> None:
        values = {}
        for kind, default in ((MIDWEEK, DEFAULT_MIDWEEK_TIME), (WEEKEND, DEFAULT_WEEKEND_TIME)):
            day, time = self._meeting_schedule_settings.slot_values(kind)
            values[f"{kind}Day"] = day
            values[f"{kind}Time"] = time if parse_time_text(time) is not None else default
            values[f"{kind}TimeEnabled"] = day != UNCONFIGURED_WEEKDAY
        self.publish(**values, backgroundSongDescription=self._background_song_description())

    def _set_schedule(self, key: str, value) -> None:
        kind = MIDWEEK if key.startswith(MIDWEEK) else WEEKEND
        day = int(value) if key.endswith("Day") else self._state[f"{kind}Day"]
        time = str(value) if key.endswith("Time") else self._state[f"{kind}Time"]
        if key.endswith("Time") and day == UNCONFIGURED_WEEKDAY:
            return
        if day not in range(-1, 7) or parse_time_text(time) is None:
            return
        self._meeting_schedule_settings.set_slot(kind, day, time if day >= 0 else "")
        self._refresh_schedule()
        self.meeting_schedule_changed.emit()

    @Slot(str)
    def searchCongregation(self, text: str) -> None:  # noqa: N802 - QML API
        if self._closed:
            return
        query = text.strip()
        self._congregation_status_kind = (
            "searching" if len(query) >= MINIMUM_QUERY_LENGTH else "idle"
        )
        self.publish(
            congregationQuery=text,
            congregationSuggestions=[],
            congregationStatusKind=self._congregation_status_kind,
        )
        self._refresh_congregation_status()
        self._congregation_lookup.search(text)

    @Slot(str, str)
    def chooseCongregation(self, guid: str, name: str) -> None:  # noqa: N802 - QML API
        if self._closed or not guid:
            return
        self._congregation_schedule = None
        self._congregation_status_kind = "resolving"
        self.publish(
            congregationName=name,
            congregationQuery=name,
            congregationSuggestions=[],
            congregationStatusKind="resolving",
        )
        self._refresh_congregation_status()
        self._congregation_lookup.fetch_schedule(guid)

    def _on_congregation_suggestions(self, matches: list) -> None:
        if self._closed:
            return
        if len(self._state["congregationQuery"].strip()) < MINIMUM_QUERY_LENGTH:
            self._congregation_status_kind = "idle"
            suggestions = []
        else:
            self._congregation_status_kind = "idle" if matches else "empty"
            suggestions = [
                {
                    "guid": match.guid,
                    "name": match.name,
                    "label": match.formatted_name or match.name,
                    "description": (
                        match.name if match.formatted_name and match.formatted_name != match.name
                        else ""
                    ),
                }
                for match in matches
            ]
        self.publish(
            congregationSuggestions=suggestions,
            congregationStatusKind=self._congregation_status_kind,
        )
        self._refresh_congregation_status()

    def _on_congregation_schedule(self, schedule) -> None:
        if self._closed:
            return
        slots = [slot for slot in schedule.slots if slot.is_configured] if schedule else []
        if not slots:
            self._congregation_schedule = None
            self._congregation_status_kind = "unpublished"
            self.publish(congregationStatusKind="unpublished")
            self._refresh_congregation_status()
            return
        for slot in slots:
            self._meeting_schedule_settings.set_slot(
                slot.kind,
                slot.weekday,
                slot.time_text,
            )
        self._congregation_schedule = schedule
        self._congregation_status_kind = "filled"
        self._refresh_schedule()
        self.publish(congregationStatusKind="filled", congregationSuggestions=[])
        self._refresh_congregation_status()
        self.meeting_schedule_changed.emit()

    def _on_congregation_failed(self, reason: str) -> None:
        if self._closed:
            return
        self._congregation_status_kind = (
            "rate_limited" if reason == RATE_LIMITED else "error"
        )
        self.publish(
            congregationSuggestions=[],
            congregationStatusKind=self._congregation_status_kind,
        )
        self._refresh_congregation_status()

    def _refresh_congregation_status(self) -> None:
        if self._congregation_status_kind == "filled" and self._congregation_schedule:
            weekdays = meeting_weekday_names()
            text = "  ·  ".join(
                f"{weekdays[slot.weekday]} {slot.time_text}"
                for slot in self._congregation_schedule.slots
                if slot.is_configured
            )
        else:
            source = _CONGREGATION_STATUS_SOURCES.get(
                self._congregation_status_kind,
                _CONGREGATION_HINT,
            )
            text = QCoreApplication.translate("SettingsWidget", source)
        self.publish(
            congregationStatusKind=self._congregation_status_kind,
            congregationStatusText=text,
        )

    def _background_song_description(self) -> str:
        if not self._meeting_schedule_settings.load().has_configured_slot:
            return QCoreApplication.translate(
                "SettingsWidget",
                "Configure the meeting day/time before automatic playback can start.",
            )
        return QCoreApplication.translate(
            "SettingsWidget",
            "Plays audio songs before configured meetings and fades out before start.",
        )

    def _sync_playback_protection(self) -> None:
        if not self._closed:
            self.publish(playbackProtection=self._playback_protection.enabled)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self._dialog_parent,
            QCoreApplication.translate("SettingsWidget", "Select folder to link"),
            self._watched_folder_settings.path() or os.path.expanduser("~"),
        )
        if path and not self._closed:
            self._watched_folder_settings.set_path(path)
            self.publish(watchedFolder=self._watched_folder_settings.path())
            self.watched_folder_changed.emit(self._watched_folder_settings.path())

    def _clear_folder(self) -> None:
        self._watched_folder_settings.clear_path()
        self.publish(watchedFolder="")
        self.watched_folder_changed.emit("")

    def _on_languages_ready(self, _languages) -> None:
        if not self._closed:
            self.publish(mediaLanguagesError="")
            self.refresh_language()

    def _on_languages_loading(self) -> None:
        if not self._closed:
            self.publish(mediaLanguagesLoading=True, mediaLanguagesError="")

    def _on_languages_failed(self, message: str) -> None:
        if not self._closed:
            self.publish(mediaLanguagesLoading=False, mediaLanguagesError=message)

    def start_deferred_services(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self.lang.jw_lang_service.fetch_if_needed()
        self._check_yeartext()

    def _on_language_switched(self, _code: str) -> None:
        if self._closed:
            return
        self._expected_yeartext.clear()
        self._fallback_requested = False
        self._yeartext_result = None
        self.publish(yeartextQuote="", yeartextReference="", yeartextDirty=False,
                     yeartextPreview="", yeartextError="", yeartextStatus="idle")
        self.refresh_language()
        self.yearly_text_changed.emit("", "", self._current_api_code())
        if self._started:
            self._check_yeartext()

    def _current_api_code(self) -> str:
        return self.media_api_code

    @property
    def media_api_code(self) -> str:
        """Effective media language, including the interface-language default."""
        return jw_media_language_context(self.lang).api_code

    def _check_yeartext(self) -> None:
        code, year = self._current_api_code(), datetime.now().year
        cached = self._yt_service.get_cached(code, year)
        if cached:
            self._accept_yeartext(code, year, *cached)
        else:
            self._request_yeartext(code, year)

    def _refresh_yeartext(self) -> None:
        self._fallback_requested = False
        self._request_yeartext(self._current_api_code(), datetime.now().year)

    def _request_yeartext(self, code: str, year: int) -> None:
        self._expected_yeartext.add((code, year))
        self.publish(yeartextStatus="loading", yeartextError="")
        self._refresh_yeartext_status()
        self._yt_service.fetch_async(code, year)

    def _on_fetch_started(self, code: str, year: int) -> None:
        if not self._closed and (code, year) in self._expected_yeartext:
            self.publish(yeartextStatus="loading", yeartextError="")
            self._refresh_yeartext_status()

    def _on_yeartext_fetched(self, code: str, year: int, quote: str, reference: str) -> None:
        if self._closed or (code, year) not in self._expected_yeartext:
            return
        self._expected_yeartext.clear()
        self._accept_yeartext(code, year, quote, reference)

    def _on_yeartext_failed(self, code: str, year: int, message: str) -> None:
        if self._closed or (code, year) not in self._expected_yeartext:
            return
        self._expected_yeartext.discard((code, year))
        fallback = jw_media_language_context(self.lang).fallback_code
        if code != fallback and not self._fallback_requested:
            self._fallback_requested = True
            cached = self._yt_service.get_cached(fallback, year)
            if cached:
                self._accept_yeartext(fallback, year, *cached)
            else:
                self._request_yeartext(fallback, year)
            return
        self.publish(yeartextStatus="error", yeartextError=message)
        self._refresh_yeartext_status()

    def _accept_yeartext(self, code: str, year: int, quote: str, reference: str) -> None:
        try:
            self._yeartext_settings.set_text(quote, reference)
        except (OSError, ValueError, RuntimeError) as exc:
            self.publish(yeartextStatus="error", yeartextError=str(exc))
            self._refresh_yeartext_status()
            return
        self._yeartext_result = (code, year, quote, reference)
        updates = {"yeartextStatus": "success", "yeartextError": ""}
        if not self._state["yeartextDirty"]:
            updates.update(yeartextQuote=quote, yeartextReference=reference)
        self.publish(**updates)
        self._refresh_yeartext_status()
        self.yearly_text_changed.emit(quote, reference, code)

    def _refresh_yeartext_status(self) -> None:
        status = self._state.get("yeartextStatus")
        if status == "loading":
            self.publish(yeartextStatusText=QCoreApplication.translate(
                "SettingsWidget", "Fetching annual text…"
            ))
        elif status == "error":
            self.publish(yeartextStatusText=QCoreApplication.translate(
                "SettingsWidget", "Could not fetch annual text"
            ))
        elif status == "success" and self._yeartext_result is not None:
            _, year, quote, reference = self._yeartext_result
            self.publish(
                yeartextStatusText=QCoreApplication.translate(
                    "SettingsWidget", "Annual text updated for {year}"
                ).replace("{year}", str(year)),
                yeartextPreview=f"{quote}  —  {reference}" if reference else quote,
            )

    def _save_yeartext(self) -> None:
        quote = self._state["yeartextQuote"].strip()
        reference = self._state["yeartextReference"].strip()
        code, year = self._current_api_code(), datetime.now().year
        self._yeartext_settings.set_text(quote, reference)
        self._yt_service.override_cache(code, year, quote, reference)
        self._expected_yeartext.clear()
        self._yeartext_result = (code, year, quote, reference)
        self.publish(yeartextQuote=quote, yeartextReference=reference, yeartextDirty=False,
                     yeartextStatus="success", yeartextError="")
        self._refresh_yeartext_status()
        self.yearly_text_changed.emit(quote, reference, code)
        self.succeed(QCoreApplication.translate("SettingsWidget", "Saved"))

    def _cancel_yeartext(self) -> None:
        quote, reference = self._yeartext_settings.text()
        self.publish(yeartextQuote=quote, yeartextReference=reference, yeartextDirty=False)

    def get_yearly_text(self) -> tuple[str, str]:
        return self._yeartext_settings.text()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._expected_yeartext.clear()
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()
        self._congregation_lookup.shutdown()
        self._yt_service.shutdown()
