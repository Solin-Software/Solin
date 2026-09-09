from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from solin.core.jw.languages import JWLanguageService
from solin.core.jw.yeartext import YeartextService
from solin.core.jw.yeartext_content import Yeartext, YeartextFetchError
from solin.core.storage.json_files import read_json_file, write_json_atomic


def _application() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def test_jw_language_cache_uses_only_the_injected_file(tmp_path) -> None:
    _application()
    primary = tmp_path / "primary" / "jw_languages.json"
    unrelated = tmp_path / "unrelated" / "jw_languages.json"
    languages = [
        {
            "code": "E",
            "vernacular": "English",
            "name": "English",
        }
    ]

    service = JWLanguageService(cache_file=primary)
    service._save_cache(languages)

    assert primary.is_file()
    assert not unrelated.exists()
    assert read_json_file(primary)["languages"] == languages
    assert JWLanguageService(cache_file=primary).languages == languages


def test_jw_language_cache_validity_is_read_from_injected_file(tmp_path) -> None:
    _application()
    cache_file = tmp_path / "jw_languages.json"
    service = JWLanguageService(cache_file=cache_file)
    service._save_cache([{"code": "E", "vernacular": "English"}])

    assert service._is_cache_valid()

    payload = read_json_file(cache_file)
    payload["_fetched_at"] = time.time() - (31 * 86400)
    write_json_atomic(cache_file, payload)

    assert not service._is_cache_valid()


def test_yeartext_cache_is_isolated_and_persisted_atomically(tmp_path) -> None:
    _application()
    primary = tmp_path / "primary" / "yeartext_cache.json"
    unrelated = tmp_path / "unrelated" / "yeartext_cache.json"

    service = YeartextService(cache_file=primary)
    service.override_cache("E", 2026, "A quote", "Matthew 5:3")

    assert primary.is_file()
    assert not unrelated.exists()
    assert YeartextService(cache_file=primary).get_cached("E", 2026) == (
        "A quote",
        "Matthew 5:3",
    )
    assert YeartextService(cache_file=unrelated).get_cached("E", 2026) is None


def test_yeartext_shutdown_is_bounded_and_discards_late_results(
    tmp_path,
    monkeypatch,
) -> None:
    application = _application()
    started = threading.Event()
    release = threading.Event()

    def blocked_fetch(api_code: str, year: int) -> Yeartext:
        started.set()
        assert release.wait(1.0)
        return Yeartext(api_code, year, "Late quote", "Late reference")

    monkeypatch.setattr("solin.core.jw.yeartext.fetch_yeartext", blocked_fetch)
    service = YeartextService(cache_file=tmp_path / "yeartext.json")
    fetched: list[tuple[object, ...]] = []
    service.fetched.connect(lambda *values: fetched.append(values))
    service.fetch_async("E", 2026)
    assert started.wait(1.0)

    unfinished = service.shutdown(timeout=0.01)
    release.set()
    deadline = time.monotonic() + 1.0
    while service._workers.active_count and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()

    assert unfinished == ("yeartext-E",)
    assert service._workers.active_count == 0
    assert fetched == []
    assert service.get_cached("E", 2026) is None


def test_jw_cache_services_do_not_import_mutable_path_globals() -> None:
    from solin.core.jw import languages, yeartext

    for module in (languages, yeartext):
        source = module.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        assert "core.foundation import paths" not in text


@pytest.mark.parametrize("late_error", [False, True])
def test_manual_yeartext_save_wins_over_pending_fetch(tmp_path, monkeypatch, late_error):
    application = _application()
    started = threading.Event()
    release = threading.Event()

    def blocked_fetch(api_code, year):
        started.set()
        assert release.wait(2.0)
        if late_error:
            raise YeartextFetchError("Network failed")
        return Yeartext(api_code, year, "Network text", "Network reference")

    monkeypatch.setattr("solin.core.jw.yeartext.fetch_yeartext", blocked_fetch)
    path = tmp_path / "yeartext.json"
    service = YeartextService(cache_file=path)
    fetched = []
    failures = []
    service.fetched.connect(lambda *args: fetched.append(args))
    service.fetch_failed.connect(lambda *args: failures.append(args))
    try:
        service.fetch_async("E", 2026)
        assert started.wait(1.0)
        service.override_cache("E", 2026, "Manual text", "Manual reference")
        release.set()
        deadline = time.monotonic() + 1.0
        while service._workers.active_count and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        application.processEvents()

        assert service.get_cached("E", 2026) == ("Manual text", "Manual reference")
        assert read_json_file(path)["E"]["quote"] == "Manual text"
        assert fetched == []
        assert failures == []
        assert not service.is_fetching("E")
    finally:
        release.set()
        service.shutdown()


def test_refresh_after_manual_save_is_not_cleared_by_older_completion(tmp_path, monkeypatch):
    application = _application()
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    requests = []

    def blocked_fetch(api_code, year):
        index = len(requests)
        requests.append(index)
        started[index].set()
        assert release[index].wait(2.0)
        return Yeartext(api_code, year, f"Network text {index}", "Reference")

    monkeypatch.setattr("solin.core.jw.yeartext.fetch_yeartext", blocked_fetch)
    service = YeartextService(cache_file=tmp_path / "yeartext.json")
    fetched = []
    service.fetched.connect(lambda *args: fetched.append(args))
    try:
        service.fetch_async("E", 2026)
        assert started[0].wait(1.0)
        service.override_cache("E", 2026, "Manual text", "Reference")
        service.fetch_async("E", 2026)
        assert started[1].wait(1.0)
        release[0].set()
        deadline = time.monotonic() + 1.0
        while service._workers.active_count > 1 and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        application.processEvents()
        assert service.is_fetching("E")
        assert service.get_cached("E", 2026) == ("Manual text", "Reference")
        assert fetched == []

        release[1].set()
        deadline = time.monotonic() + 1.0
        while service._workers.active_count and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        application.processEvents()
        assert not service.is_fetching("E")
        assert service.get_cached("E", 2026) == ("Network text 1", "Reference")
        assert fetched == [("E", 2026, "Network text 1", "Reference")]
    finally:
        for event in release:
            event.set()
        service.shutdown()
