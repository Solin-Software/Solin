from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QObject, Signal

from solin.core.foundation.settings_store import ProfileAppSettingsStore, SettingsStore
from solin.core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from solin.core.jw.background_song_settings import BackgroundSongSettingsStore
from solin.core.jw.yeartext_settings import YeartextSettingsStore
from solin.core.media.settings import MediaSettingsStore
from solin.core.meetings.schedule import MIDWEEK, WEEKEND, MeetingSchedule, MeetingSlot
from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore
from solin.ui.qml.settings import general as general_module
from solin.ui.qml.settings.general import GeneralSettings


class Languages(QObject):
    languages_ready = Signal(list)
    media_language_changed = Signal(str)
    fetch_started = Signal()
    fetch_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.languages = [{"code": "T", "vernacular": "Português", "name": "Portuguese"},
                          {"code": "E", "name": "English"}]
        self.media_api_code = "T"
        self.is_loading = False
        self.fetch_count = 0

    def get_language(self, code):
        return next((item for item in self.languages if item["code"] == code), None)

    def set_media_api_code(self, code):
        self.media_api_code = code
        self.media_language_changed.emit(code)

    def fetch_if_needed(self):
        self.fetch_count += 1

    def force_refresh(self):
        self.fetch_count += 1


class LanguageManager(QObject):
    language_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.jw_lang_service = Languages()
        self.current_code = "en"
        self.api_code = "E"
        self.meta = {"name": "English"}

    def available_languages(self):
        return [("en", "English"), ("pt", "Português")]

    def set_language(self, code):
        self.current_code = code
        self.meta = {"name": dict(self.available_languages())[code]}
        self.api_code = "T" if code == "pt" else "E"
        self.language_changed.emit(code)


class Screens(QObject):
    screens_changed = Signal()

    @staticmethod
    def primary_screen():
        return None

    @staticmethod
    def secondary_screens():
        return []


class Protection(QObject):
    enabledChanged = Signal()

    def __init__(self):
        super().__init__()
        self.enabled = True

    def set_enabled(self, value):
        self.enabled = value
        self.enabledChanged.emit()


class YeartextService(QObject):
    fetched = Signal(str, int, str, str)
    fetch_failed = Signal(str, int, str)
    fetch_started = Signal(str, int)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.cache = {}
        self.shutdown_count = 0

    def get_cached(self, code, year):
        return self.cache.get((code, year))

    def fetch_async(self, code, year):
        self.requests.append((code, year))
        self.fetch_started.emit(code, year)

    def override_cache(self, code, year, quote, reference):
        self.cache[(code, year)] = quote, reference

    def shutdown(self):
        self.shutdown_count += 1


class CongregationLookup(QObject):
    suggestions_ready = Signal(list)
    schedule_ready = Signal(object)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.searches = []
        self.schedule_requests = []
        self.shutdown_count = 0

    def search(self, query):
        self.searches.append(query)

    def fetch_schedule(self, guid):
        self.schedule_requests.append(guid)

    def shutdown(self):
        self.shutdown_count += 1


@pytest.fixture
def setup():
    store = SettingsStore.for_namespace(f"qml_settings_{uuid4().hex}", "tests")
    language = LanguageManager()
    yeartext = YeartextService()
    congregation_lookup = CongregationLookup()
    protection = Protection()
    screens = Screens()
    app_settings = ProfileAppSettingsStore(store)
    settings = GeneralSettings(
        lang_manager=language, screen_manager=screens,
        app_settings=app_settings,
        media_settings=MediaSettingsStore(store), playback_protection=protection,
        meeting_schedule_settings=MeetingScheduleSettingsStore(store),
        watched_folder_settings=WatchedFolderSettingsStore(store),
        yeartext_settings=YeartextSettingsStore(store),
        background_song_settings=BackgroundSongSettingsStore(store),
        yeartext_service_factory=lambda _parent: yeartext,
        congregation_lookup_factory=lambda _parent: congregation_lookup,
    )
    yield SimpleNamespace(settings=settings, store=store, language=language,
                          yeartext=yeartext, congregation_lookup=congregation_lookup,
                          protection=protection, screens=screens)
    settings.cleanup()
    store.clear()


def test_service_starts_once_and_persists_before_any_qml_page(setup):
    settings = setup.settings
    received = []
    settings.yearly_text_changed.connect(lambda *args: received.append(args))
    settings.start_deferred_services()
    settings.start_deferred_services()
    year = datetime.now().year
    assert setup.yeartext.requests == [("T", year)]
    assert setup.language.jw_lang_service.fetch_count == 1
    setup.yeartext.fetched.emit("T", year, "Annual text", "Isaiah 41:10")
    assert settings.get_yearly_text() == ("Annual text", "Isaiah 41:10")
    assert settings.state["yeartextStatus"] == "success"
    assert received == [("Annual text", "Isaiah 41:10", "T")]


def test_dirty_drafts_survive_service_result_and_retranslation(setup):
    settings = setup.settings
    settings.start_deferred_services()
    settings.setValue("yeartextQuote", "Draft quote")
    settings.setValue("yeartextReference", "Draft reference")
    setup.yeartext.fetched.emit("T", datetime.now().year, "Fetched quote", "Fetched reference")
    settings.refresh_runtime()
    settings.refresh_language()
    assert settings.state["yeartextQuote"] == "Draft quote"
    assert settings.state["yeartextReference"] == "Draft reference"
    assert settings.get_yearly_text() == ("Fetched quote", "Fetched reference")
    settings.invoke("cancelYeartext")
    assert settings.state["yeartextQuote"] == "Fetched quote"
    assert settings.state["yeartextDirty"] is False


def test_manual_save_ignores_pending_network_result(setup):
    settings = setup.settings
    settings.start_deferred_services()
    settings.setValue("yeartextQuote", "  Manual text  ")
    settings.setValue("yeartextReference", "  Reference  ")
    settings.invoke("saveYeartext")
    setup.yeartext.fetched.emit("T", datetime.now().year, "Network text", "Network reference")
    assert settings.get_yearly_text() == ("Manual text", "Reference")
    assert settings.state["yeartextDirty"] is False
    assert settings.state["feedbackKind"] == "success"


def test_fallback_and_old_language_responses_are_isolated(setup):
    settings = setup.settings
    settings.start_deferred_services()
    year = datetime.now().year
    setup.yeartext.fetch_failed.emit("T", year, "Unavailable")
    assert setup.yeartext.requests == [("T", year), ("E", year)]
    setup.yeartext.fetched.emit("E", year, "Fallback text", "Reference")
    assert settings.get_yearly_text() == ("Fallback text", "Reference")
    settings.setValue("mediaLanguage", "E")
    setup.yeartext.fetched.emit("T", year, "Stale text", "Stale reference")
    assert settings.get_yearly_text() == ("Fallback text", "Reference")
    setup.yeartext.fetch_failed.emit("E", year, "Offline")
    assert settings.state["yeartextStatus"] == "error"
    assert settings.state["yeartextError"] == "Offline"


def test_cleanup_disconnects_shared_services_and_rejects_late_results(setup):
    settings = setup.settings
    settings.start_deferred_services()
    before = settings.state
    settings.cleanup()
    settings.cleanup()
    setup.yeartext.fetched.emit("T", datetime.now().year, "Late", "Result")
    setup.language.jw_lang_service.media_language_changed.emit("E")
    setup.protection.set_enabled(False)
    settings.setValue("yeartextQuote", "Changed")
    settings.start_deferred_services()
    assert settings.state == before
    assert setup.yeartext.shutdown_count == 1
    assert setup.congregation_lookup.shutdown_count == 1


def test_congregation_lookup_fills_schedule_and_reports_state(setup):
    settings = setup.settings
    lookup = setup.congregation_lookup
    schedule_store = MeetingScheduleSettingsStore(setup.store)
    changed = []
    settings.meeting_schedule_changed.connect(lambda: changed.append(True))

    settings.searchCongregation("Bet")
    assert lookup.searches == ["Bet"]
    assert settings.state["congregationStatusKind"] == "idle"

    settings.searchCongregation("Bethel")
    assert settings.state["congregationStatusKind"] == "searching"
    match = SimpleNamespace(
        guid="congregation-id",
        name="Bethel",
        formatted_name="Bethel · São Paulo",
    )
    lookup.suggestions_ready.emit([match])
    assert settings.state["congregationSuggestions"] == [{
        "guid": "congregation-id",
        "name": "Bethel",
        "label": "Bethel · São Paulo",
        "description": "Bethel",
    }]

    settings.chooseCongregation("congregation-id", "Bethel")
    assert lookup.schedule_requests == ["congregation-id"]
    assert settings.state["congregationStatusKind"] == "resolving"
    lookup.schedule_ready.emit(MeetingSchedule(
        midweek=MeetingSlot(MIDWEEK, 2, 19 * 60 + 30),
        weekend=MeetingSlot(WEEKEND, 5, 10 * 60),
    ))

    assert schedule_store.slot_values(MIDWEEK) == (2, "19:30")
    assert schedule_store.slot_values(WEEKEND) == (5, "10:00")
    assert settings.state["congregationStatusKind"] == "filled"
    assert "19:30" in settings.state["congregationStatusText"]
    assert "10:00" in settings.state["congregationStatusText"]
    assert changed == [True]


def test_congregation_lookup_reports_empty_unpublished_and_failure(setup):
    settings = setup.settings
    lookup = setup.congregation_lookup

    settings.searchCongregation("Unknown")
    lookup.suggestions_ready.emit([])
    assert settings.state["congregationStatusKind"] == "empty"

    settings.chooseCongregation("congregation-id", "Unknown")
    lookup.schedule_ready.emit(None)
    assert settings.state["congregationStatusKind"] == "unpublished"

    settings.searchCongregation("Unknown")
    lookup.failed.emit("rate_limited")
    assert settings.state["congregationStatusKind"] == "rate_limited"
    lookup.failed.emit("unavailable")
    assert settings.state["congregationStatusKind"] == "error"


def test_schedule_validation_persistence_and_background_hint(setup):
    settings = setup.settings
    schedule = MeetingScheduleSettingsStore(setup.store)
    emitted = []
    settings.meeting_schedule_changed.connect(lambda: emitted.append(True))
    initial_hint = settings.state["backgroundSongDescription"]
    settings.setValue("midweekDay", 2)
    settings.setValue("midweekTime", "19:35")
    assert schedule.slot_values("midweek") == (2, "19:35")
    assert settings.state["backgroundSongDescription"] != initial_hint
    settings.setValue("midweekTime", "25:60")
    settings.setValue("midweekDay", 8)
    assert schedule.slot_values("midweek") == (2, "19:35")
    assert len(emitted) == 2
    settings.setValue("midweekDay", -1)
    assert schedule.slot_values("midweek") == (-1, "")
    assert settings.state["backgroundSongDescription"] == initial_hint


@pytest.mark.parametrize("kind", ["midweek", "weekend"])
def test_meeting_time_availability_tracks_configured_day(setup, kind):
    settings = setup.settings
    emitted = []
    settings.meeting_schedule_changed.connect(lambda: emitted.append(True))

    assert settings.state[f"{kind}TimeEnabled"] is False
    settings.setValue(f"{kind}Time", "08:15")
    assert emitted == []

    settings.setValue(f"{kind}Day", 0)
    assert settings.state[f"{kind}TimeEnabled"] is True

    settings.setValue(f"{kind}Day", -1)
    assert settings.state[f"{kind}TimeEnabled"] is False


def test_media_options_persist_and_external_protection_syncs(setup):
    settings = setup.settings
    emitted = []
    settings.meetings_auto_download_toggled.connect(emitted.append)
    for key in ("autoDownloadOnPlay", "meetingsAutoDownload", "songAnnouncement", "startVideosPaused"):
        settings.setValue(key, True)
    media = MediaSettingsStore(setup.store)
    assert media.auto_download_on_play()
    assert media.meetings_auto_download()
    assert media.sjjm_announce_mode()
    assert media.start_videos_paused()
    assert emitted == [True]
    setup.protection.set_enabled(False)
    assert settings.state["playbackProtection"] is False


def test_save_error_has_no_success_feedback_or_signal(setup, monkeypatch):
    settings = setup.settings
    settings.setValue("yeartextQuote", "Draft")
    emitted = []
    settings.yearly_text_changed.connect(lambda *args: emitted.append(args))

    def fail_save(*_args):
        raise OSError("Storage unavailable")

    monkeypatch.setattr(YeartextSettingsStore, "set_text", fail_save)
    settings.invoke("saveYeartext")
    assert settings.state["feedbackKind"] == "error"
    assert settings.state["yeartextDirty"] is True
    assert emitted == []


def test_media_language_default_and_error_recovery(setup):
    settings = setup.settings
    settings.setValue("mediaLanguage", "")
    assert settings.media_api_code == "E"
    assert settings.state["mediaLanguage"] == ""
    setup.language.jw_lang_service.fetch_failed.emit("Offline")
    assert settings.state["mediaLanguagesError"] == "Offline"
    setup.language.jw_lang_service.languages_ready.emit([])
    assert settings.state["mediaLanguagesError"] == ""


def test_no_monitor_is_a_valid_runtime_state(setup):
    assert setup.settings.state["screens"] == []
    setup.screens.screens_changed.emit()
    assert setup.settings.state["screens"] == []


def test_theme_selection_and_refresh_preserve_runtime_and_unsaved_draft(setup, monkeypatch):
    settings = setup.settings
    monkeypatch.setattr(general_module, "current_theme", lambda: SimpleNamespace(id="dark"))
    settings.setValue("yeartextQuote", "Unsaved text")
    settings.setValue("midweekDay", 3)
    settings.setValue("midweekTime", "19:30")
    settings.setValue("playbackProtection", False)
    emissions = []
    settings.theme_changed.connect(emissions.append)
    settings.setValue("themeId", "light")
    settings.refresh_language()
    settings.refresh_runtime()
    assert ProfileAppSettingsStore(setup.store).app_theme_id() == "light"
    assert settings.state["themeId"] == "light"
    assert settings.state["yeartextQuote"] == "Unsaved text"
    assert settings.state["yeartextDirty"] is True
    assert settings.state["midweekDay"] == 3
    assert settings.state["midweekTime"] == "19:30"
    assert settings.state["playbackProtection"] is False
    assert emissions == ["light"]

    settings.setValue("themeId", "unsupported-theme")
    assert ProfileAppSettingsStore(setup.store).app_theme_id() == "light"
    monkeypatch.setattr(general_module, "current_theme", lambda: SimpleNamespace(id="light"))
    settings.setValue("themeId", "light")
    assert emissions == ["light"]


def test_interface_language_uses_public_language_manager_and_emits_once(setup):
    settings = setup.settings
    selected = []
    settings.language_changed.connect(selected.append)
    settings.setValue("interfaceLanguage", "pt")
    assert setup.language.current_code == "pt"
    assert settings.state["interfaceLanguage"] == "pt"
    assert settings.state["interfaceLanguageName"] == "Português"
    assert selected == ["pt"]
    settings.setValue("interfaceLanguage", "pt")
    settings.setValue("interfaceLanguage", "unknown")
    assert selected == ["pt"]
    setup.language.set_language("en")
    assert settings.state["interfaceLanguage"] == "en"
    assert settings.state["interfaceLanguageName"] == "English"
    assert selected == ["pt"]


def test_cached_yeartext_is_published_without_network_start(setup):
    setup.yeartext.cache[("T", datetime.now().year)] = ("Cached text", "Reference")
    emissions = []
    setup.settings.yearly_text_changed.connect(lambda *args: emissions.append(args))
    setup.settings.start_deferred_services()
    assert setup.yeartext.requests == []
    assert emissions == [("Cached text", "Reference", "T")]
    assert setup.settings.state["yeartextQuote"] == "Cached text"
    assert setup.settings.get_yearly_text() == ("Cached text", "Reference")
