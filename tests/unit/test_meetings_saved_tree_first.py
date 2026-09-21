from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import solin.widgets.meetings.widget as meetings_widget_module
from solin.core.meetings.models import WeekData
from solin.core.meetings.preparation import (
    MeetingPreparationKey,
    MeetingPreparationPhase,
    MeetingPreparationPriority,
    MeetingPreparationState,
)
from solin.core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
)
from solin.core.ingest.sync.journal import ReplicaSnapshot
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


def _preparation(*, week_data: WeekData | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        week_data=lambda _key: week_data,
        state=lambda _key, _pub_type: MeetingPreparationState(
            MeetingPreparationPhase.LOADING
        ),
    )


def test_overview_uses_saved_tree_when_week_data_is_not_loaded() -> None:
    monday = date(2026, 5, 25)
    overview = _overview()
    widget = SimpleNamespace(
        _cache={},
        _overview=overview,
        _saved_snapshots_for=lambda _monday: {"mwb": _snapshot("mwb")},
        _preparation=_preparation(),
        _preparation_key=lambda monday_arg: MeetingPreparationKey(monday_arg, "T"),
        _apply_unavailable_card_state=lambda card, _monday, _pub: card.set_loading(),
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
        _refresh_overview_cards=lambda _monday: None,
        _saved_snapshot_for=lambda pub_type, _monday: (
            _snapshot(pub_type) if pub_type == "mwb" else None
        ),
        _preparation_key=lambda monday_arg: MeetingPreparationKey(monday_arg, "T"),
    )
    key = MeetingPreparationKey(monday, "T")

    MeetingsWidget._on_preparation_progress(widget, key, "mwb", 42)
    MeetingsWidget._on_preparation_error(widget, key, "mwb", "No URL")

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
        _preparation=_preparation(),
        _preparation_key=lambda monday_arg: MeetingPreparationKey(monday_arg, "T"),
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


def test_ready_week_data_still_opens_the_persisted_snapshot() -> None:
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
        _preparation=_preparation(week_data=wd),
        _preparation_key=lambda monday_arg: MeetingPreparationKey(monday_arg, "T"),
        _show_study_detail=show_detail,
        _navbar=SimpleNamespace(setVisible=lambda _visible: None),
        _stack=SimpleNamespace(setCurrentWidget=lambda _detail: None),
    )

    MeetingsWidget._open_detail(widget, "mwb")

    assert captured["saved_snapshot"] is snapshot


def test_detail_save_invalidates_cached_saved_snapshot_before_next_open() -> None:
    monday = date(2026, 5, 25)
    old_snapshot = _snapshot("mwb", title="Before edit")
    updated_snapshot = _snapshot("mwb", title="After edit")
    cache_key = f"{monday.isoformat()}:T:spoken"
    refreshed: list[date] = []

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[str, date, str, bool]] = []

        def snapshots_for_week(
            self,
            monday_arg: date,
            language: str,
            is_sign_language: bool = False,
        ) -> dict[str, MeetingTreeSnapshot]:
            self.calls.append(("week", monday_arg, language, is_sign_language))
            return {"mwb": updated_snapshot}

    store = Store()
    widget = SimpleNamespace(
        _saved_snapshots={cache_key: {"mwb": old_snapshot}},
        _current_media_context=lambda: SimpleNamespace(
            api_code="T",
            is_sign_language=False,
        ),
        _meeting_tree_store=store,
        _monday=monday,
        _saved_snapshot_cache_key_for=lambda monday_arg, language, is_sign=False: (
            f"{monday_arg.isoformat()}:{language}:"
            f"{'sign' if is_sign else 'spoken'}"
        ),
        _saved_snapshot_cache_key=lambda monday_arg: (
            f"{monday_arg.isoformat()}:T:spoken"
        ),
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
    assert store.calls == [("week", monday, "T", False)]


def test_missing_canonical_media_recovery_is_sent_to_preparation_service() -> None:
    calls: list[tuple[MeetingPreparationKey, str, str]] = []
    widget = SimpleNamespace(
        _preparation=SimpleNamespace(
            recover_missing_canonical_media=lambda key, pub_type, source: calls.append(
                (key, pub_type, source)
            )
        )
    )

    MeetingsWidget._on_canonical_media_recovery_requested(
        widget,
        "mwb:2026-05-25:T:20260500",
        "C:/cache/jwpub/mwb_T/x_20260500/image.jpg",
    )

    assert calls == [
        (
            MeetingPreparationKey(date(2026, 5, 25), "T"),
            "mwb",
            "C:/cache/jwpub/mwb_T/x_20260500/image.jpg",
        )
    ]


def test_show_detail_observes_tree_saved_during_detail_construction(monkeypatch) -> None:
    monday = date(2026, 5, 25)
    old_snapshot = _snapshot("mwb", title="Before constructor save")
    cache_key = f"{monday.isoformat()}:T:spoken"
    refreshed: list[date] = []
    added = []

    class FakeStudyDetailView:
        def __init__(self, pub_type, _wd, **kwargs) -> None:
            self.back_requested = _ConnectSignal()
            self.play_requested = _ConnectSignal()
            self.media_destination_requested = _ConnectSignal()
            self.set_as_idle_requested = _ConnectSignal()
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
        _current_media_context=lambda: SimpleNamespace(
            api_code="T",
            is_sign_language=False,
        ),
        _tree_controller_factory=object(),
        _document_conversion_service=object(),
        _jw_catalog_service_factory=object(),
        _jw_catalog_thumbnail_session_factory=object(),
        _jw_songs_store=object(),
        _watched_folder="",
        _saved_snapshots={cache_key: {"mwb": old_snapshot}},
        _saved_snapshot_cache_key_for=lambda monday_arg, language, is_sign=False: (
            f"{monday_arg.isoformat()}:{language}:"
            f"{'sign' if is_sign else 'spoken'}"
        ),
        _monday=monday,
        _refresh_overview_cards=lambda refreshed_monday: refreshed.append(refreshed_monday),
        _on_detail_tree_saved=lambda tree_key: MeetingsWidget._on_detail_tree_saved(
            widget,
            tree_key,
        ),
        _on_canonical_media_recovery_requested=lambda _tree_key, _source_path: None,
        _on_detail_back=lambda: None,
        project_media=lambda _media: None,
        media_destination_requested=_ConnectSignal(),
        set_as_idle_requested=_ConnectSignal(),
        _stack=SimpleNamespace(addWidget=lambda detail: added.append(detail)),
        _details={},
    )

    MeetingsWidget._show_study_detail(
        widget,
        "mwb",
        WeekData(monday=monday),
        old_snapshot,
    )

    assert cache_key not in widget._saved_snapshots
    assert refreshed == [monday]
    assert len(added) == 1
    assert widget._details[f"mwb:{monday.isoformat()}"] is added[0]


def test_controller_load_saved_tree_uses_snapshot_nodes_without_saving_canonical() -> None:
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None,
        _request_sync_refresh=lambda: None,
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


def test_saved_sync_binding_survives_restart_without_cloud_folder(tmp_path) -> None:
    from solin.core.ingest.sync.journal import ReplicaSnapshot
    binding = {
        "folder": str(tmp_path / "missing-cloud" / "2026-05-25 MW"),
        "enabled": True,
        "snapshot": ReplicaSnapshot().to_dict(),
    }
    store_path = tmp_path / "meetings.json"
    writer = MeetingTreeStore(store_path)
    writer.save("mwb:2026-05-25:T:20260500", _snapshot().nodes, "hash", linked_sync=binding)
    snapshot = MeetingTreeStore(store_path).snapshot("mwb:2026-05-25:T:20260500")
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None,
        _request_sync_refresh=lambda: None,
        _start_media_requests=lambda: None,
        _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(), syncStateChanged=_Signal(), stateChanged=_Signal(),
    )
    MeetingTreeController.load_saved_tree(controller, snapshot)
    assert controller._sync_enabled
    assert controller._sync_folder == binding["folder"]
    assert controller._sync_snapshot == ReplicaSnapshot()
    # Metadata enrichment must not silently discard the linkage.
    writer.save(snapshot.tree_key, snapshot.nodes, "new hash")
    assert MeetingTreeStore(store_path).snapshot(snapshot.tree_key).linked_sync == binding


def test_restarted_meeting_accepts_offline_edit_before_cloud_returns(tmp_path) -> None:
    from solin.core.meetings.linked_folder_sync import MeetingLinkedFolderSync, MeetingSyncIdentity
    folder = tmp_path / "cloud" / "2026-05-25 MW"
    folder.mkdir(parents=True)
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:20260500", "mwb", date(2026, 5, 25))
    args = dict(deleted_source_keys=set(), linked_folder_files={}, meeting_folder_imports={})
    original = MeetingLinkedFolderSync(lambda _: 0)
    initial = original.save_tree(
        folder,
        identity,
        nodes=_snapshot().nodes,
        enable=True,
        **args,
    )
    store_path = tmp_path / "meetings.json"
    MeetingTreeStore(store_path).save(identity.tree_key, initial.nodes, "hash", linked_sync={
        "folder": str(folder), "enabled": True, "snapshot": initial.snapshot.to_dict(),
    })
    original.save_tree(folder, identity, nodes=[
        *initial.nodes, {"id": "queued-before-crash", "type": "media", "children": []},
    ], base_snapshot=initial.snapshot, stage_only=True, **args)
    parked = tmp_path / "offline-folder"
    assert folder.resolve().is_relative_to(tmp_path.resolve())
    assert parked.resolve().is_relative_to(tmp_path.resolve())
    folder.rename(parked)
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None, _request_sync_refresh=lambda: None,
        _start_media_requests=lambda: None, _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(), syncStateChanged=_Signal(), stateChanged=_Signal(),
    )
    MeetingTreeController.load_saved_tree(controller, MeetingTreeStore(store_path).snapshot(identity.tree_key))
    assert controller._sync_enabled
    restarted = MeetingLinkedFolderSync(lambda _: 0)
    recovered = restarted.resume_local_tree(folder, identity, controller._sync_snapshot)
    assert {node["id"] for node in recovered.nodes} == {"media", "queued-before-crash"}
    controller._nodes = recovered.nodes
    controller._sync_snapshot = recovered.snapshot
    controller._nodes[0]["title"] = "Edited offline after restart"
    staged = restarted.save_tree(
        folder, identity, nodes=controller._nodes, base_snapshot=controller._sync_snapshot,
        stage_only=True, **args,
    )
    assert staged.pending_count
    parked.rename(folder)
    loaded = restarted.load_tree(str(folder.parent), identity)
    assert loaded.nodes[0]["title"] == "Edited offline after restart"
    assert not loaded.pending_count


def test_controller_can_defer_media_enrichment_until_detail_is_visible() -> None:
    enrichment_calls: list[str] = []
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None,
        _request_sync_refresh=lambda: None,
        _start_media_requests=lambda: enrichment_calls.append("started"),
        _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(),
        syncStateChanged=_Signal(),
        stateChanged=_Signal(),
    )

    MeetingTreeController.load_saved_tree(
        controller,
        _snapshot("wt"),
        start_media_requests=False,
    )

    assert enrichment_calls == []

    MeetingTreeController.start_media_enrichment(controller)

    assert enrichment_calls == ["started"]


def test_controller_flushes_pending_framing_and_reloads_latest_revision(tmp_path) -> None:
    store = MeetingTreeStore(tmp_path / "meeting_trees.json")
    incoming = store.save(
        "mwb:2026-05-25:T:20260500",
        [
            {
                "id": "image",
                "type": "media",
                "children": [],
                "image_framing": {"scale": 1.0},
            }
        ],
        "hash",
        overview=MeetingTreeOverview(title="Meeting", media_count=1),
    )
    timer = SimpleNamespace(stopped=False)
    timer.stop = lambda: setattr(timer, "stopped", True)
    controller = SimpleNamespace(
        _store=store,
        _nodes=[
            {
                "id": "image",
                "type": "media",
                "children": [],
                "image_framing": {"scale": 1.4},
            }
        ],
        _tree_key=incoming.tree_key,
        _canonical_hash=incoming.canonical_hash,
        _deleted_source_keys=set(),
        _linked_folder_files={},
        _meeting_folder_imports={},
        _overview=incoming.overview,
        _image_framing_save_pending=True,
        _image_framing_save_timer=timer,
        _refresh_sync_availability=lambda: None,
        _request_sync_refresh=lambda: None,
        _start_media_requests=lambda: None,
        _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(),
        syncStateChanged=_Signal(),
        stateChanged=_Signal(),
    )

    def save_pending_framing() -> bool:
        controller._image_framing_save_pending = False
        store.save(
            controller._tree_key,
            controller._nodes,
            controller._canonical_hash,
            controller._deleted_source_keys,
            controller._linked_folder_files,
            controller._meeting_folder_imports,
            controller._overview,
        )
        return True

    controller._save = save_pending_framing

    loaded = MeetingTreeController.load_saved_tree(controller, incoming)

    assert loaded is not None
    assert loaded.revision == 2
    assert controller._nodes[0]["image_framing"] == {"scale": 1.4}
    assert timer.stopped is True


def test_controller_merges_fresh_resolution_into_pending_manifest_publish() -> None:
    snapshot = _snapshot("mwb")
    snapshot.nodes[:] = [
        {
            "id": "official",
            "type": "media",
            "children": [],
            "meeting_generated": True,
            "meeting_source_key": "media:mwb:official",
            "media_ref": {"key_symbol": "mwbv", "track": 1},
            "resolved_url": "https://cdn.example/fresh.mp4",
        }
    ]
    pending = SimpleNamespace(
        nodes=[
            {
                "id": "official",
                "type": "media",
                "children": [],
                "meeting_generated": True,
                "meeting_source_key": "media:mwb:official",
                "media_ref": {"key_symbol": "mwbv", "track": 1},
                "resolved_url": "https://cdn.example/stale.mp4",
                "start_trim_ticks": 10,
            }
        ],
        deleted_source_keys=set(),
        linked_folder_files={},
        meeting_folder_imports={},
        folder="C:/tmp/2026-05-25 MW",
        expected_revision=4,
        snapshot=ReplicaSnapshot(),
    )
    save_events: list[str] = []
    controller = SimpleNamespace(
        _refresh_sync_availability=lambda: None,
        _pending_sync_save_for_identity=lambda _identity: pending,
        _linked_folder_availability_signature=lambda: (),
        _start_media_requests=lambda: None,
        _image_framing_save_pending=False,
        _image_framing_save_timer=SimpleNamespace(stop=lambda: None),
        _schedule_sync_manifest_save=lambda: save_events.append("publish"),
        _save_local_cache=lambda: save_events.append("local") or True,
        _meeting_folder_pending_sources=set(),
        chromeChanged=_Signal(),
        syncStateChanged=_Signal(),
        stateChanged=_Signal(),
    )
    controller._save = lambda: MeetingTreeController._save(controller)

    MeetingTreeController.load_saved_tree(controller, snapshot)

    assert controller._nodes[0]["resolved_url"] == "https://cdn.example/fresh.mp4"
    assert controller._nodes[0]["start_trim_ticks"] == 10
    assert save_events == ["local"]


def test_automatic_download_requests_current_and_next_week_through_preparation() -> None:
    requests = []
    widget = SimpleNamespace(
        _media_settings=SimpleNamespace(meetings_auto_download=lambda: True),
        _ensure_week=lambda monday, **options: requests.append((monday, options)),
    )

    MeetingsWidget.sync_automatic_downloads(widget)

    assert len(requests) == 2
    assert (requests[1][0] - requests[0][0]).days == 7
    assert all(options["download_media"] is True for _, options in requests)
    assert all(
        options["priority"] is MeetingPreparationPriority.BACKGROUND
        for _, options in requests
    )


def test_initial_shell_schedules_automatic_downloads_before_snapshot_result() -> None:
    monday = date(2026, 5, 25)
    calls: list[str] = []
    widget = SimpleNamespace(
        _overview=SimpleNamespace(finish_build=lambda: calls.append("overview")),
        _navbar=SimpleNamespace(
            update_week=lambda monday_arg: calls.append(f"navbar:{monday_arg}")
        ),
        _monday=monday,
        _loading_placeholder=SimpleNamespace(
            finish=lambda: calls.append("placeholder")
        ),
        _auto_download_timer=SimpleNamespace(
            start=lambda interval: calls.append(f"timer:{interval}")
        ),
        _snapshot_preparation=SimpleNamespace(
            start=lambda: calls.append("snapshot")
        ),
        update=lambda: calls.append("update"),
    )

    MeetingsWidget._finish_initial_shell(widget)

    assert calls == [
        "overview",
        f"navbar:{monday}",
        "placeholder",
        "timer:3000",
        "snapshot",
        "update",
    ]


def test_destination_empty_state_is_terminal_instead_of_loading() -> None:
    monday = date(2026, 5, 25)
    widget = SimpleNamespace(
        _preparation=SimpleNamespace(
            state=lambda _key, _pub_type: MeetingPreparationState(
                MeetingPreparationPhase.IDLE,
                source_status="empty",
            )
        ),
        _preparation_key=lambda monday_arg: MeetingPreparationKey(monday_arg, "T"),
    )

    target = MeetingsWidget._destination_target(
        widget,
        "mwb",
        monday,
        None,
        None,
    )

    assert target["status"] == "unavailable"
    assert target["available"] is False
