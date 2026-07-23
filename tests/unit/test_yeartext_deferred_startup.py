from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from solin.widgets.settings.yearly_text_section import YearlyTextSectionMixin
from solin.widgets.settings_widget import SettingsWidget


class _SignalRecorder:
    def __init__(self) -> None:
        self.emissions: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.emissions.append(args)


class _YeartextServiceStub:
    def __init__(self) -> None:
        self.fetches: list[tuple[str, int]] = []

    def get_cached(self, _api_code: str, _year: int):
        return None

    def fetch_async(self, api_code: str, year: int) -> None:
        self.fetches.append((api_code, year))


class _YeartextSettingsStub:
    def __init__(self) -> None:
        self.values: list[tuple[str, str]] = []

    def set_text(self, quote: str, reference: str) -> None:
        self.values.append((quote, reference))


class _YeartextHarness(YearlyTextSectionMixin):
    def __init__(self) -> None:
        self._ui_ready = False
        self._deferred_services_started = True
        self._yt_status_kind = "loading"
        self._yeartext_result = None
        self._yeartext_error_message = ""
        self._yt_service = _YeartextServiceStub()
        self._yeartext_settings = _YeartextSettingsStub()
        self.yearly_text_changed = _SignalRecorder()
        self.rendered: list[tuple[str, int, str, str]] = []

    @staticmethod
    def _current_api_code() -> str:
        return "T"

    @staticmethod
    def _fallback_api_code() -> str:
        return "E"

    @staticmethod
    def _current_year() -> int:
        return 2026

    def _render_yeartext_success(
        self,
        api_code: str,
        year: int,
        quote: str,
        reference: str,
    ) -> None:
        self.rendered.append((api_code, year, quote, reference))


def test_settings_services_start_before_deferred_ui_is_ready() -> None:
    language_fetches: list[str] = []
    yeartext_checks: list[str] = []
    widget = SimpleNamespace(
        _deferred_services_started=False,
        _ui_ready=False,
        lang=SimpleNamespace(
            jw_lang_service=SimpleNamespace(
                fetch_if_needed=lambda: language_fetches.append("language")
            )
        ),
        _check_and_fetch_yeartext=lambda: yeartext_checks.append("yeartext"),
    )

    SettingsWidget.start_deferred_services(cast(Any, widget))
    SettingsWidget.start_deferred_services(cast(Any, widget))

    assert widget._deferred_services_started is True
    assert language_fetches == ["language"]
    assert yeartext_checks == ["yeartext"]


def test_yeartext_fetch_starts_without_settings_ui() -> None:
    section = _YeartextHarness()

    section._check_and_fetch_yeartext()

    assert section._yt_service.fetches == [("T", 2026)]
    assert section._yt_status_kind == "loading"


def test_yeartext_result_is_persisted_then_rendered_when_ui_becomes_ready() -> None:
    section = _YeartextHarness()

    section._on_yeartext_fetched("T", 2026, "The annual text", "Isaiah 41:10")

    assert section._yeartext_settings.values == [
        ("The annual text", "Isaiah 41:10")
    ]
    assert section.yearly_text_changed.emissions == [
        ("The annual text", "Isaiah 41:10", "T")
    ]
    assert section.rendered == []

    section._ui_ready = True
    section._sync_yeartext_ui()

    assert section.rendered == [
        ("T", 2026, "The annual text", "Isaiah 41:10")
    ]
