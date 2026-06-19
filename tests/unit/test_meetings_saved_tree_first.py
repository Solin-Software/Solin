from __future__ import annotations

from datetime import date
from types import SimpleNamespace

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


def _snapshot(pub_type: str = "mwb") -> MeetingTreeSnapshot:
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
        overview=MeetingTreeOverview(title="Saved meeting", media_count=1),
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
