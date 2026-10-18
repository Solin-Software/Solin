"""About preferences, asynchronous pagination and consumer lifetime contracts."""

from __future__ import annotations

from threading import Event, current_thread, main_thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QSignalSpy

from solin.core.foundation.settings_store import InstallationSettingsStore, SettingsStore
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.release_history import ReleaseHistoryCursorError
from solin.ui.qml.settings.about import AboutSettings
from tests._qt import dispose_qobject, wait_until


def _page(*versions, next_cursor=None, language="en"):
    return SimpleNamespace(items=[
        SimpleNamespace(version=ReleaseVersion.parse(version),
                        markdown=f"## {version}\n\nNotes in {language}.", language=language)
        for version in versions
    ], next_cursor=next_cursor)


@pytest.fixture
def make_about(request):
    def create(installed="26.33.0b2", channel=UpdateChannel.STABLE, handler=None):
        store = SettingsStore.for_namespace(f"about_settings_{uuid4().hex}", "tests")
        settings = InstallationSettingsStore(store)
        settings.set_update_channel(channel)
        backend = SimpleNamespace(calls=[], cancellations=[], threads=set(), gates=[], language="en")
        backend.handler = handler or (lambda **kwargs: _page("26.32.0"))

        def factory(cancelled):
            backend.cancellations.append(cancelled)

            def history(**kwargs):
                backend.threads.add(current_thread())
                backend.calls.append(kwargs)
                return backend.handler(**kwargs)

            return SimpleNamespace(history=history)

        about = AboutSettings(settings, factory, lambda: backend.language, installed)

        def cleanup():
            about.cleanup()
            for gate in backend.gates:
                gate.set()
            for thread in backend.threads:
                thread.join(timeout=3)
                assert not thread.is_alive(), "About worker did not stop"
            dispose_qobject(about)
            store.clear()

        request.addfinalizer(cleanup)
        return about, settings, backend

    return create


def _ready(about):
    wait_until(lambda: not about.state["historyBusy"], description="release history completion")


def test_preferences_persist_independently_and_emit_only_actual_channel_changes(make_about):
    about, settings, backend = make_about()
    changed = QSignalSpy(about.update_channel_changed)
    assert about.state["installedVersion"] == "26.33.0-beta.2"
    assert about.state["installedIsBeta"]
    assert not about.state["betaUpdatesEnabled"]
    assert not about.state["includeBetaNotes"]
    about.setValue("betaUpdatesEnabled", False)
    about.setValue("betaUpdatesEnabled", True)
    about.setValue("betaUpdatesEnabled", True)
    assert changed.count() == 1
    assert settings.update_channel() == UpdateChannel.BETA
    assert not settings.release_history_include_beta()
    about.setValue("includeBetaNotes", True)
    assert settings.release_history_include_beta()
    assert changed.count() == 1
    about.setValue("betaUpdatesEnabled", False)
    assert settings.release_history_include_beta()
    assert about.state["installedIsBeta"]
    assert backend.calls == []


@pytest.mark.parametrize("version,is_beta", [("26.33.0", False), ("26.33.0b2", True)])
def test_installed_identity_is_independent_of_channel(make_about, version, is_beta):
    about, _, _ = make_about(installed=version, channel=UpdateChannel.BETA)
    assert about.state["installedIsBeta"] is is_beta
    assert about.state["betaUpdatesEnabled"]


def test_history_is_lazy_and_runs_conversion_off_ui_thread(make_about, monkeypatch):
    from solin.ui.qml.settings import about as module

    converted_on = []
    render = module.release_notes_html

    def convert(markdown):
        converted_on.append(current_thread())
        return render(markdown)

    monkeypatch.setattr(module, "release_notes_html", convert)
    about, _, backend = make_about(installed="26.32.0")
    about.refresh_language()
    about.loadMore()
    about.retry()
    assert backend.calls == []
    about.activate()
    about.activate()
    _ready(about)
    assert backend.calls == [dict(include_beta=False, language="en", cursor=None, page_size=5)]
    assert converted_on and converted_on[0] is not main_thread()
    entry, = about.state["historyEntries"]
    assert entry["isInstalled"] and not entry["expanded"]
    assert "Notes in en." in entry["html"]
    assert not about.state["historyHasMore"]
    about.activate()
    about.loadMore()
    assert len(backend.calls) == 1


def test_pagination_retains_expansions_and_avoids_duplicate_entries(make_about):
    def history(**kwargs):
        if kwargs["cursor"] is None:
            return _page("26.35.0", "26.34.0", "26.33.0", "26.32.0", "26.31.0",
                         next_cursor="page-2")
        return _page("26.31.0", "26.30.0")

    about, _, backend = make_about(handler=history)
    about.activate()
    _ready(about)
    assert len(about.state["historyEntries"]) == 5
    assert all(not entry["expanded"] for entry in about.state["historyEntries"])
    about.setReleaseExpanded("26.34.0", True)
    before = about.state["historyEntries"]
    about.loadMore()
    about.loadMore()
    _ready(about)
    assert about.state["historyEntries"][:5] == before
    assert len(about.state["historyEntries"]) == 6
    assert not about.state["historyEntries"][-1]["expanded"]
    assert backend.calls[-1]["cursor"] == "page-2"
    assert not about.state["historyHasMore"]


def test_filter_cancels_pending_request_and_rejects_old_delivery(make_about):
    gate = Event()

    def history(**kwargs):
        if not kwargs["include_beta"]:
            assert gate.wait(3)
            return _page("26.32.0")
        return _page("26.33.0b2")

    about, settings, backend = make_about(handler=history)
    backend.gates.append(gate)
    about.activate()
    wait_until(lambda: len(backend.calls) == 1, description="first history request")
    about.setValue("includeBetaNotes", True)
    assert backend.cancellations[0].is_set()
    _ready(about)
    gate.set()
    for thread in backend.threads:
        thread.join(timeout=3)
    assert settings.update_channel() == UpdateChannel.STABLE
    assert [entry["version"] for entry in about.state["historyEntries"]] == ["26.33.0-beta.2"]
    about.setValue("includeBetaNotes", True)
    assert len(backend.calls) == 2


def test_language_change_cancels_and_resets_history(make_about):
    gate = Event()

    def history(**kwargs):
        if kwargs["language"] == "en":
            assert gate.wait(3)
        return _page("26.32.0", language=kwargs["language"])

    about, _, backend = make_about(handler=history)
    backend.gates.append(gate)
    about.activate()
    wait_until(lambda: len(backend.calls) == 1, description="English history request")
    backend.language = "pt"
    about.refresh_language()
    _ready(about)
    gate.set()
    assert backend.cancellations[0].is_set()
    assert about.state["historyEntries"][0]["language"] == "pt"
    assert backend.calls[-1]["cursor"] is None


def test_language_changed_before_host_retranslation_rejects_already_queued_result(make_about):
    about, _, backend = make_about(handler=lambda **kwargs: _page("26.32.0", language=kwargs["language"]))
    about.activate()
    handle = about._load
    handle._future.result(timeout=3)
    backend.language = "pt"
    _ready(about)
    assert [call["language"] for call in backend.calls] == ["en", "pt"]
    assert about.state["historyEntries"][0]["language"] == "pt"


def test_partial_failure_preserves_entries_and_retries_same_cursor(make_about):
    attempts = 0

    def history(**kwargs):
        nonlocal attempts
        if kwargs["cursor"] is None:
            return _page("26.32.0", next_cursor="more")
        attempts += 1
        if attempts == 1:
            raise OSError("offline")
        return _page("26.31.0")

    about, _, backend = make_about(handler=history)
    about.activate()
    _ready(about)
    entries = about.state["historyEntries"]
    about.loadMore()
    _ready(about)
    assert about.state["historyError"]
    assert about.state["historyEntries"] == entries
    about.retry()
    _ready(about)
    assert not about.state["historyError"]
    assert len(about.state["historyEntries"]) == 2
    assert [call["cursor"] for call in backend.calls] == [None, "more", "more"]


def test_initial_failure_can_retry_to_an_empty_loaded_history(make_about):
    attempts = 0

    def history(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("offline")
        return _page()

    about, _, _ = make_about(handler=history)
    about.activate()
    _ready(about)
    assert about.state["historyError"] and not about.state["historyLoaded"]
    about.retry()
    _ready(about)
    assert about.state["historyLoaded"] and not about.state["historyEntries"]
    assert not about.state["historyError"]


@pytest.mark.parametrize("restart_fails", [False, True])
def test_expired_cursor_restarts_first_page_and_replaces_only_on_success(make_about, restart_fails):
    first_page_calls = 0

    def history(**kwargs):
        nonlocal first_page_calls
        if kwargs["cursor"] is not None:
            raise ReleaseHistoryCursorError("expired")
        first_page_calls += 1
        if first_page_calls == 1:
            return _page("26.32.0", next_cursor="expired")
        if restart_fails and first_page_calls == 2:
            raise OSError("offline")
        return _page("26.34.0", "26.33.0", next_cursor="fresh")

    about, _, backend = make_about(handler=history)
    about.activate()
    _ready(about)
    previous = about.state["historyEntries"]
    about.loadMore()
    _ready(about)
    if restart_fails:
        assert about.state["historyEntries"] == previous
        assert about.state["historyError"]
        about.retry()
        _ready(about)
    assert [entry["version"] for entry in about.state["historyEntries"]] == ["26.34.0", "26.33.0"]
    assert all(not entry["expanded"] for entry in about.state["historyEntries"])
    assert not about.state["historyError"]
    expected = [None, "expired", None] + (["expired", None] if restart_fails else [])
    assert [call["cursor"] for call in backend.calls] == expected


def test_cleanup_cancels_workers_and_prevents_all_mutations(make_about):
    gate = Event()

    def history(**kwargs):
        assert gate.wait(3)
        return _page("26.32.0")

    about, settings, backend = make_about(handler=history)
    backend.gates.append(gate)
    about.activate()
    wait_until(lambda: len(backend.calls) == 1, description="history worker")
    about.cleanup()
    assert backend.cancellations[0].is_set()
    snapshot = about.state
    about.setValue("betaUpdatesEnabled", True)
    about.setValue("includeBetaNotes", True)
    about.activate()
    about.retry()
    about.loadMore()
    about.refresh_language()
    about.setReleaseExpanded("26.32.0", True)
    gate.set()
    for thread in backend.threads:
        thread.join(timeout=3)
    assert settings.update_channel() == UpdateChannel.STABLE
    assert about.state == snapshot


@pytest.mark.parametrize("url,allowed", [
    ("https://example.com/notes", True), ("http://localhost:4000/notes", True),
    ("http://example.com", False), ("file:///private", False),
    ("javascript:alert(1)", False), ("https://[", False),
])
def test_note_links_use_the_validated_remote_opener(make_about, monkeypatch, url, allowed):
    about, _, _ = make_about()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda value: opened.append(value.toString()))
    about.openLink(url)
    assert opened == ([url] if allowed else [])
