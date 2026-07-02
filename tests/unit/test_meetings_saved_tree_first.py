from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import solin.widgets.meetings.widget as meetings_widget_module
from solin.core.meetings.models import WeekData
from solin.core.meetings.tree_store import MeetingTreeOverview, MeetingTreeSnapshot
from solin.widgets.meetings.tree_controller import MeetingTreeController
from solin.widgets.meetings.widget import MeetingsWidget


class _Card:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def set_saved(self, snapshot) -> None:
        self.calls.append(("saved", snapshot.tree_key))

    def set_loading(self) -> None:
        self.calls.append(("loading", None))

    def set_ready(self, wd) -> None:
        self.calls.append(("ready", wd.monday))

    def set_empty(self) -> None:
        self.calls.append(("empty", None))

    def set_not_found(self) -> None:
        self.calls.append(("not_found", None))

    def set_error(self) -> None:
        self.calls.append(("error", None))

    def set_progress(self, pct: int) -> None:
        self.calls.append(("progress", pct))


class _MemorialCard:
    def __init__(self) -> None:
        self.visible: list[bool] = []

    def setVisible(self, visible: bool) -> None:
        self.visible.append(visible)


class _Signal:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, *args) -> None:
        self.calls += 1


class _ConnectSignal:
    def __init__(self) -> None:
        self.handlers = []

    def connect(self, handler) -> None:
        self.handlers.append(handler)


def _snapshot(
    pub_type: str = "mwb",
    *,
    title: str = "Saved meeting",
) -> MeetingTreeSnapshot:
    monday = date(2026, 5, 25)
    return MeetingTreeSnapshot(
        tree_key=f"{pub_type}:2026-05-25:T:20260500",
        pub_type=pub_type,
        monday=monday,
        language="T",
        issue="20260500",
        nodes=[{"id": "media", "type": "media", "children": []}],
        canonical_hash="hash",
        deleted_source_keys=set(),
        linked_folder_files={},
        meeting_folder_imports={},
        overview=MeetingTreeOverview(title=title, media_count=1),
    )


def _overview() -> SimpleNamespace:
    return SimpleNamespace(
        mwb_card=_Card(),
        wt_card=_Card(),
        memorial_card=_MemorialCard(),
    )


def test_overview_uses_saved_tree_when_week_data_is_not_loaded() -> None:
    monday = date(2026, 5, 25)
    overview = _overview()
    widget = SimpleNamespace(
        _cache={},
        _overview=overview,
        _saved_snapshots_for=lambda _monday: {"mwb": _snapshot("mwb")},
        _memorial_svc=SimpleNamespace(is_memorial_week=lambda _monday: False),
    )

    MeetingsWidget._refresh_overview_cards(widget, monday)

    assert overview.mwb_card.calls == [("saved", "mwb:2026-05-25:T:20260500")]
    assert overview.wt_card.calls == [("loading", None)]
    assert overview.memorial_card.visible == [False]


def test_saved_tree_prevents_progress_and_error_from_replacing_card() -> None:
    monday = date(2026, 5, 25)
    overview = _overview()
    widget = SimpleNamespace(
        _monday=monday,
        _overview=overview,
        destinationTargetsChanged=SimpleNamespace(emit=lambda _key: None),
        _saved_snapshot_for_key=lambda pub_type, _key: (
            _snapshot(pub_type) if pub_type == "mwb" else None
        ),
    )

    MeetingsWidget._on_progress(widget, monday.isoformat(), "mwb", 42)
    MeetingsWidget._on_error(widget, monday.isoformat(), "mwb", "No URL")

    assert overview.mwb_card.calls == []


def test_open_detail_uses_saved_snapshot_without_week_data() -> None:
    monday = date(2026, 5, 25)
    snapshot = _snapshot("mwb")
    captured = {}

    def show_detail(pub_type, wd, saved_snapshot):
        captured["pub_type"] = pub_type
        captured["wd"] = wd
        captured["saved_snapshot"] = saved_snapshot
        widget._details[f"{pub_type}:{wd.monday.isoformat()}"] = "detail"

    widget = SimpleNamespace(
        _monday=monday,
        _cache={},
        _details={},
        _saved_snapshot_for=lambda _pub_type, _monday: snapshot,
        _show_study_detail=show_detail,
        _navbar=SimpleNamespace(setVisible=lambda visible: captured.setdefault("visible", visible)),
        _stack=SimpleNamespace(setCurrentWidget=lambda detail: captured.setdefault("current", detail)),
    )

    MeetingsWidget._open_detail(widget, "mwb")

    assert captured["pub_type"] == "mwb"
    assert captured["wd"].monday == monday
    assert captured["saved_snapshot"] is snapshot
    assert captured["visible"] is False
    assert captured["current"] == "detail"


def test_ready_week_data_uses_canonical_path_even_when_snapshot_exists() -> None:
    monday = date(2026, 5, 25)
    snapshot = _snapshot("mwb")
    wd = WeekData(monday=monday, mwb_status="ready")
    captured = {}

    def show_detail(pub_type, detail_wd, saved_snapshot):
        captured["saved_snapshot"] = saved_snapshot
        widget._details[f"{pub_type}:{detail_wd.monday.isoformat()}"] = "detail"

    widget = SimpleNamespace(
        _monday=monday,
        _cache={monday.isoformat(): wd},
        _details={},
        _saved_snapshot_for=lambda _pub_type, _monday: snapshot,
        _show_study_detail=show_detail,
        _navbar=SimpleNamespace(setVisible=lambda _visible: None),
        _stack=SimpleNamespace(setCurrentWidget=lambda _detail: None),
    )

    MeetingsWidget._open_detail(widget, "mwb")

    assert captured["saved_snapshot"] is None


def test_detail_save_invalidates_cached_saved_snapshot_before_next_open() -> None:
    monday = date(2026, 5, 25)
    old_snapshot = _snapshot("mwb", title="Before edit")
    updated_snapshot = _snapshot("mwb", title="After edit")
    cache_key = f"{monday.isoformat()}:T"
    refreshed: list[date] = []

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[str, date, str]] = []

        def find_snapshot(
            self,
            pub_type: str,
            monday_arg: date,
            language: str,
        ) -> MeetingTreeSnapshot | None:
            self.calls.append((pub_type, monday_arg, language))
            if pub_type == "mwb":
                return updated_snapshot
            return None

    store = Store()
    widget = SimpleNamespace(
        _saved_snapshots={cache_key: {"mwb": old_snapshot}},
        _current_media_context=lambda: SimpleNamespace(api_code="T"),
        _meeting_tree_store=store,
        _monday=monday,
        _saved_snapshot_cache_key_for=lambda monday_arg, language: (
            f"{monday_arg.isoformat()}:{language}"
        ),
        _saved_snapshot_cache_key=lambda monday_arg: f"{monday_arg.isoformat()}:T",
        _refresh_saved_snapshots=lambda monday_arg: MeetingsWidget._refresh_saved_snapshots(
            widget,
            monday_arg,
        ),
        _refresh_overview_cards=lambda refreshed_monday: refreshed.append(refreshed_monday),
    )

    MeetingsWidget._on_detail_tree_saved(widget, old_snapshot.tree_key)

    assert cache_key not in widget._saved_snapshots
    assert refreshed == [monday]
    assert MeetingsWidget._saved_snapshots_for(widget, monday)["mwb"] is updated_snapshot
    assert store.calls == [("mwb", monday, "T"), ("wt", monday, "T")]


def test_show_detail_observes_tree_saved_during_detail_construction(monkeypatch) -> None:
    monday = date(2026, 5, 25)
    old_snapshot = _snapshot("mwb", title="Before constructor save")
    cache_key = f"{monday.isoformat()}:T"
    refreshed: list[date] = []
    added = []

    class FakeStudyDetailView:
        def __init__(self, pub_type, _wd, **kwargs) -> None:
            self.back_requested = _ConnectSignal()
            self.play_requested = _ConnectSignal()
            handler = kwargs["meeting_tree_saved_handler"]
            assert handler is not None
            handler(_snapshot(pub_type, title="Saved during construction").tree_key)

    monkeypatch.setattr(
        meetings_widget_module,
        "StudyDetailView",
        FakeStudyDetailView,
    )
    widget = SimpleNamespace(
        _notifications=None,
        _playback_protection=object(),
        _current_media_context=lambda: SimpleNamespace(api_code="T"),
        _tree_controller_factory=object(),
        _document_conversion_service=object(),
        _jw_catalog_service_factory=object(),
        _jw_catalog_thumbnail_session_factory=object(),
        _jw_songs_store=object(),
        _watched_folder="",
        _saved_snapshots={cache_key: {"mwb": old_snapshot}},
        _saved_snapshot_cache_key_for=lambda monday_arg, language: (
            f"{monday_arg.isoformat()}:{language}"
        ),
        _monday=monday,
        _refresh_overview_cards=lambda refreshed_monday: refreshed.append(refreshed_monday),
        _on_detail_tree_saved=lambda tree_key: MeetingsWidget._on_detail_tree_saved(
            widget,
            tree_key,
        ),
        _on_detail_back=lambda: None,
        project_media=lambda _media: None,
        _stack=SimpleNamespace(addWidget=lambda detail: added.append(detail)),
        _details={},
    )

    MeetingsWidget._show_study_detail(widget, "mwb", WeekData(monday=monday), None)

    assert cache_key not in widget._saved_snapshots
    assert refreshed == [monday]
    assert len(added) == 1
    assert widget._details[f"mwb:{monday.isoformat()}"] is added[0]


def test_controller_load_saved_tree_uses_snapshot_nodes_without_saving_canonical() -> None:
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None,
        _load_sync_record=lambda: None,
        _candidate_sync_folder=lambda: "",
        _linked_folder_availability_signature=lambda: (),
        _start_media_requests=lambda: None,
        _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(),
        syncStateChanged=_Signal(),
        stateChanged=_Signal(),
    )
    snapshot = _snapshot("wt")

    MeetingTreeController.load_saved_tree(controller, snapshot)

    assert controller._tree_key == snapshot.tree_key
    assert controller._nodes == snapshot.nodes
    assert controller._overview == snapshot.overview
    assert controller.chromeChanged.calls == 1
    assert controller.syncStateChanged.calls == 1
    assert controller.stateChanged.calls == 1
