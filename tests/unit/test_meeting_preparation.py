from __future__ import annotations

import time

import pytest
from datetime import date

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot

import solin.core.meetings.preparation as preparation_module
from solin.core.meetings.canonical_restore import canonical_tree_diff
from solin.core.meetings.models import MeetingMedia, WeekData
from solin.core.meetings.content_policy import (
    MeetingContentError,
    MeetingContentOperation,
    MeetingRequestOrigin,
    MeetingContentPolicy,
)
from solin.core.meetings.preparation import (
    MeetingPreparationKey,
    MeetingPreparationPriority,
    MeetingPreparationRequest,
    MeetingPreparationService,
    _automatic_download_urls,
)
from solin.core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
)
from solin.core.meetings.tree_types import clone_nodes, iter_nodes
from solin.core.jw.language_context import JWMediaLanguageContext


_APP = QCoreApplication.instance() or QCoreApplication([])


class _PublicationService(QObject):
    mwb_ready = Signal(str, object)
    wt_ready = Signal(str, object)
    cbs_ready = Signal(str, object)
    context_progress = Signal(str, str, int, str, bool, int)
    context_error = Signal(str, str, str, str, bool, int)
    context_load_finished = Signal(str, str, bool, int)

    def __init__(self) -> None:
        super().__init__()
        self.loads: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def load_week(self, monday, force=False, **context) -> None:
        self.loads.append({"monday": monday, "force": force, **context})

    def clear_week(self, _monday):
        pass

    def cancel_pending_week(self, _monday, **_context):
        pass

    def get_week_data(self, _monday, **_context):
        return None

    def shutdown(self, **_kwargs) -> None:
        self.shutdown_calls += 1


class _CacheManager:
    def __init__(self, store: MeetingTreeStore) -> None:
        self.store = store
        self.prefetch_calls: list[tuple[list[str], str]] = []
        self.cancel_calls: list[str] = []

    def prefetch_many(self, urls: list[str], batch_id: str) -> int:
        snapshots = self.store.snapshots_for_week(date(2026, 5, 25), "T")
        assert any(
            node.get("resolved_url") == "https://cdn.example/meeting.mp4"
            for snapshot in snapshots.values()
            for node in iter_nodes(snapshot.nodes)
        )
        self.prefetch_calls.append((list(urls), batch_id))
        return len(urls)

    def is_cached(self, _url: str) -> bool:
        return False

    def cancel_batch(self, batch_id: str) -> None:
        self.cancel_calls.append(batch_id)


def test_resolution_signature_separates_audio_and_video_formats() -> None:
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    identity = {
        "key_symbol": "mwbv",
        "track": 2,
        "issue_tag": 20260500,
        "meps_doc_id": 123,
    }

    audio = MeetingPreparationService._resolution_signature(
        key,
        identity | {"mime_type": "audio/mpeg"},
    )
    video = MeetingPreparationService._resolution_signature(
        key,
        identity | {"mime_type": "video/mp4"},
    )

    assert audio[2] == "audio"
    assert video[2] == "video"
    assert audio != video


class _ImmediateResolver(QObject):
    resolved = Signal(str, object)

    @Slot(str, str, int, int, int, str, str, bool)
    def resolve(self, request_id, *_args) -> None:
        self.resolved.emit(
            request_id,
            {
                "url": "https://cdn.example/meeting.mp4",
                "title": "Resolved title",
                "thumbnail": "https://cdn.example/meeting.jpg",
                "duration_ticks": 123_000_000,
            },
        )


class _DeferredResolver(QObject):
    resolved = Signal(str, object)
    latest: "_DeferredResolver | None" = None

    def __init__(self) -> None:
        super().__init__()
        self.request_id = ""
        self.args: tuple[object, ...] = ()
        type(self).latest = self

    @Slot(str, str, int, int, int, str, str, bool)
    def resolve(self, request_id, *args) -> None:
        self.request_id = request_id
        self.args = args

    def complete(self) -> None:
        self.resolved.emit(
            self.request_id,
            {
                "url": "https://cdn.example/meeting.mp4",
                "title": "Resolved title",
                "thumbnail": "",
            },
        )


def _spin_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)
    assert predicate()


def _service(monkeypatch, tmp_path, resolver_class=_ImmediateResolver):
    monkeypatch.setattr(
        preparation_module,
        "MeetingMediaResolutionWorker",
        resolver_class,
    )
    store = MeetingTreeStore(tmp_path / "meeting_trees.json")
    publication = _PublicationService()
    cache = _CacheManager(store)
    service = MeetingPreparationService(publication, store, cache)
    service._today = lambda: date(2026, 5, 25)
    return service, publication, store, cache


def _week_data(
    generation: int,
    *,
    with_media: bool,
    source_checksum: str = "",
    cbs_status: str = "idle",
) -> WeekData:
    media = (
        [
            MeetingMedia(
                key_symbol="mwbv",
                track=1,
                issue_tag=20260500,
                section="tgw",
                mime_type="video/mp4",
            )
        ]
        if with_media
        else []
    )
    return WeekData(
        monday=date(2026, 5, 25),
        language_code="T",
        request_generation=generation,
        mwb_status="ready",
        mwb_issue="20260500",
        mwb_source_checksum=source_checksum,
        mwb_date_label="May 25-31",
        mwb_all_media=media,
        cbs_status=cbs_status,
    )


def test_identical_requests_coalesce_and_upgrade_download_policy(monkeypatch, tmp_path) -> None:
    service, publication, _store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        service.ensure_week(
            MeetingPreparationRequest(
                key=key,
                download_media=True,
                priority=MeetingPreparationPriority.BACKGROUND,
            )
        )

        assert len(publication.loads) == 1
        assert service._jobs[key].download_media is True
        service.cancel_automatic_downloads()
        assert service._jobs[key].download_media is True
        assert cache.cancel_calls == []
    finally:
        service.shutdown(wait_ms=1000)


def test_tree_and_resolved_urls_are_committed_before_prefetch(monkeypatch, tmp_path) -> None:
    service, publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key, download_media=True))
        generation = int(publication.loads[0]["generation"])
        publication.mwb_ready.emit(key.monday.isoformat(), _week_data(generation, with_media=True))

        _spin_until(lambda: bool(cache.prefetch_calls))

        snapshot = store.find_snapshot("mwb", key.monday, key.language_code)
        assert snapshot is not None
        media = next(node for node in iter_nodes(snapshot.nodes) if node.get("type") == "media")
        assert media["resolved_url"] == "https://cdn.example/meeting.mp4"
        assert media["base_duration_ticks"] == 123_000_000
        assert cache.prefetch_calls[0][0] == ["https://cdn.example/meeting.mp4"]
    finally:
        service.shutdown(wait_ms=1000)


def test_canonical_image_does_not_enter_audiovisual_resolver(monkeypatch, tmp_path) -> None:
    service, publication, store, _cache = _service(
        monkeypatch,
        tmp_path,
        resolver_class=_DeferredResolver,
    )
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        week = WeekData(
            monday=key.monday,
            language_code=key.language_code,
            request_generation=generation,
            mwb_status="ready",
            mwb_issue="20260500",
            mwb_date_label="May 25-31",
            cbs_status="idle",
            mwb_all_media=[
                MeetingMedia(
                    key_symbol="mwb",
                    track=2,
                    issue_tag=20260500,
                    section="tgw",
                    mime_type="image/jpeg",
                )
            ],
        )

        publication.mwb_ready.emit(key.monday.isoformat(), week)
        _spin_until(lambda: store.find_snapshot("mwb", key.monday, key.language_code) is not None)

        assert _DeferredResolver.latest is not None
        assert _DeferredResolver.latest.request_id == ""
        snapshot = store.find_snapshot("mwb", key.monday, key.language_code)
        assert snapshot is not None
        image = next(node for node in iter_nodes(snapshot.nodes) if node.get("type") == "media")
        assert image["media_type"] == "image"
        assert not image.get("resolved_url")
    finally:
        service.shutdown(wait_ms=1000)


def test_audiovisual_metadata_without_url_still_enters_resolver(monkeypatch, tmp_path) -> None:
    service, _publication, _store, _cache = _service(
        monkeypatch,
        tmp_path,
        resolver_class=_DeferredResolver,
    )
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        job = service._jobs[key]
        snapshot = MeetingTreeSnapshot(
            tree_key="mwb:2026-05-25:T:20260500",
            pub_type="mwb",
            monday=key.monday,
            language=key.language_code,
            issue="20260500",
            nodes=[
                {
                    "id": "video-node",
                    "type": "media",
                    "media_type": "video",
                    "base_duration_ticks": 123_000_000,
                    "thumbnail_url": "https://cdn.example/thumb.jpg",
                    "meeting_generated": True,
                    "children": [],
                    "media_ref": {
                        "key_symbol": "mwbv",
                        "track": 2,
                        "issue_tag": 20260500,
                        "mime_type": "video/mp4",
                    },
                }
            ],
            canonical_hash="canonical",
            deleted_source_keys=set(),
            linked_folder_files={},
            meeting_folder_imports={},
            overview=MeetingTreeOverview(),
        )

        service._schedule_snapshot_media(job, "mwb", snapshot)
        _spin_until(lambda: bool(_DeferredResolver.latest and _DeferredResolver.latest.request_id))

        assert _DeferredResolver.latest is not None
        assert _DeferredResolver.latest.args[4] == "video"
    finally:
        service.shutdown(wait_ms=1000)


def test_automatic_download_preserves_subsection_ancestry_policy() -> None:
    shared_url = "https://cdn.example/shared.mp4"
    manual_url = "https://cdn.example/manual.mp4"
    excluded_url = "https://cdn.example/subsection-only.mp4"
    nodes = [
        {
            "id": "direct-official",
            "type": "media",
            "meeting_generated": True,
            "resolved_url": shared_url,
            "children": [],
        },
        {
            "id": "subsection",
            "type": "subsection",
            "children": [
                {
                    "id": "nested-official-shared",
                    "type": "media",
                    "meeting_generated": True,
                    "resolved_url": shared_url,
                    "children": [],
                },
                {
                    "id": "nested-official-only",
                    "type": "media",
                    "meeting_generated": True,
                    "resolved_url": excluded_url,
                    "children": [],
                },
                {
                    "id": "nested-manual",
                    "type": "media",
                    "meeting_generated": False,
                    "resolved_url": manual_url,
                    "children": [],
                },
            ],
        },
    ]

    assert _automatic_download_urls(nodes) == {shared_url, manual_url}


def test_resolved_official_subsection_media_is_persisted_without_prefetch(
    monkeypatch,
    tmp_path,
) -> None:
    service, _publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    tree_key = "mwb:2026-05-25:T:20260500"
    store.save(
        tree_key,
        [
            {
                "id": "subsection",
                "type": "subsection",
                "children": [
                    {
                        "id": "nested-official",
                        "type": "media",
                        "meeting_generated": True,
                        "meeting_source_key": "media:cbs:official",
                        "media_type": "video",
                        "media_ref": {
                            "key_symbol": "mwbv",
                            "track": 1,
                            "mime_type": "video/mp4",
                        },
                        "children": [],
                    }
                ],
            }
        ],
        "hash",
    )
    try:
        service.ensure_week(MeetingPreparationRequest(key=key, download_media=True))

        _spin_until(
            lambda: bool(
                (snapshot := store.snapshot(tree_key))
                and next(
                    node
                    for node in iter_nodes(snapshot.nodes)
                    if node.get("id") == "nested-official"
                ).get("resolved_url")
            )
        )

        snapshot = store.snapshot(tree_key)
        assert snapshot is not None
        media = next(
            node for node in iter_nodes(snapshot.nodes) if node.get("id") == "nested-official"
        )
        assert media["resolved_url"] == "https://cdn.example/meeting.mp4"
        assert media["thumbnail_url"] == "https://cdn.example/meeting.jpg"
        assert media["base_duration_ticks"] == 123_000_000
        assert cache.prefetch_calls == []
    finally:
        service.shutdown(wait_ms=1000)


def test_cached_media_missing_duration_is_enriched_without_opening_detail(
    monkeypatch,
    tmp_path,
) -> None:
    service, _publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    tree_key = "mwb:2026-05-25:T:20260500"
    store.save(
        tree_key,
        [
            {
                "id": "official",
                "type": "media",
                "meeting_generated": True,
                "meeting_source_key": "media:mwb:official",
                "media_type": "video",
                "resolved_url": "https://cdn.example/meeting.mp4",
                "media_ref": {
                    "key_symbol": "mwbv",
                    "track": 1,
                    "mime_type": "video/mp4",
                },
                "children": [],
            }
        ],
        "hash",
    )
    cache.is_cached = lambda _url: True
    try:
        service.ensure_week(MeetingPreparationRequest(key=key, download_media=True))

        _spin_until(
            lambda: bool(
                (snapshot := store.snapshot(tree_key))
                and next(iter_nodes(snapshot.nodes)).get("base_duration_ticks")
            )
        )

        snapshot = store.snapshot(tree_key)
        assert snapshot is not None
        assert next(iter_nodes(snapshot.nodes))["base_duration_ticks"] == 123_000_000
    finally:
        service.shutdown(wait_ms=1000)


def test_on_demand_preparation_persists_tree_without_prefetch(monkeypatch, tmp_path) -> None:
    service, publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        publication.mwb_ready.emit(key.monday.isoformat(), _week_data(generation, with_media=True))

        _spin_until(
            lambda: (
                (snapshot := store.find_snapshot("mwb", key.monday, "T")) is not None
                and any(node.get("resolved_url") for node in iter_nodes(snapshot.nodes))
            )
        )

        assert cache.prefetch_calls == []
    finally:
        service.shutdown(wait_ms=1000)


def test_reconciled_tree_records_the_confirmed_source_checksum(monkeypatch, tmp_path) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        publication.mwb_ready.emit(
            key.monday.isoformat(),
            _week_data(generation, with_media=False, source_checksum="confirmed"),
        )

        _spin_until(
            lambda: (
                (snapshot := store.find_snapshot("mwb", key.monday, "T")) is not None
                and snapshot.source_checksum == "confirmed"
            )
        )
    finally:
        service.shutdown(wait_ms=1000)


def test_loading_mwb_references_do_not_confirm_partial_canonical_tree(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        partial = _week_data(
            generation,
            with_media=True,
            source_checksum="confirmed",
            cbs_status="loading",
        )
        publication.mwb_ready.emit(key.monday.isoformat(), partial)

        snapshot = store.find_snapshot("mwb", key.monday, "T")
        assert snapshot is not None
        assert snapshot.nodes
        assert snapshot.canonical_nodes
        assert not snapshot.canonical_hash
        assert snapshot.source_checksum == ""

        partial.cbs_status = "ready"
        publication.cbs_ready.emit(key.monday.isoformat(), partial)

        confirmed = store.find_snapshot("mwb", key.monday, "T")
        assert confirmed is not None
        assert confirmed.canonical_nodes
        assert confirmed.source_checksum == "confirmed"
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("observed", [{}, {"mwb:T:20260500": "source"}])
def test_incomplete_base_stays_available_and_normal_checks_can_complete_it(
    monkeypatch, tmp_path, auto_update, observed
):
    from solin.core.meetings.preparation import MeetingPreparationPhase

    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=auto_update)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.prepare_meeting(key, "mwb")
        generation = int(publication.loads[-1]["generation"])
        partial = _week_data(
            generation, with_media=True, source_checksum="source", cbs_status="loading"
        )
        publication.mwb_ready.emit(key.monday.isoformat(), partial)
        partial.cbs_status = "error"
        publication.cbs_ready.emit(key.monday.isoformat(), partial)
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        state = service.state(key, "mwb")
        assert state.phase == MeetingPreparationPhase.AVAILABLE
        assert not state.repair_required
        assert not state.update_available
        assert state.snapshot is not None and state.snapshot.nodes
        assert not state.snapshot.canonical_hash
        assert len(publication.loads) == 1, "Failure must not start an immediate retry loop"

        service.maintain_prepared(key)
        check_generation = int(publication.loads[-1]["generation"])
        service._on_content_checked((key.monday, "T", False, check_generation), "mwb", observed)
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, check_generation)

        operations = [load["content_request"].operation for load in publication.loads]
        assert operations == [MeetingContentOperation.PREPARE, MeetingContentOperation.CHECK] + (
            [MeetingContentOperation.UPDATE] if auto_update else []
        )
        if not auto_update:
            service.prepare_meeting(key, "mwb", force=True)
        completed = _week_data(
            int(publication.loads[-1]["generation"]),
            with_media=True,
            source_checksum="source",
            cbs_status="ready",
        )
        publication.cbs_ready.emit(key.monday.isoformat(), completed)
        snapshot = store.find_snapshot("mwb", key.monday, "T")
        assert snapshot is not None and snapshot.canonical_hash
        assert snapshot.source_checksum == "source"
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("reference_status", ["loading", "error"])
def test_incomplete_update_applies_healthy_nodes_and_preserves_applied_revision(
    monkeypatch, tmp_path, reference_status
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    nodes = service._builder.build_midweek(_week_data(0, with_media=True))
    original = store.save(
        "mwb:2026-05-25:T:20260500",
        nodes,
        service._builder.canonical_hash(nodes),
        canonical_nodes=nodes,
        source_checksum="old",
        publication_dependencies=[
            {"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "old"},
            {"pub": "book", "language": "T", "issue": "0", "checksum": "book-old"},
        ],
    )
    try:
        service.prepare_meeting(key, "mwb", force=True)
        partial = _week_data(
            int(publication.loads[-1]["generation"]),
            with_media=False,
            source_checksum="new",
            cbs_status=reference_status,
        )
        partial.mwb_all_media = [MeetingMedia(
            multimedia_id=99, section="lac", mime_type="image/jpeg", label="Healthy new image"
        )]
        publication.cbs_ready.emit(key.monday.isoformat(), partial)
        current = store.find_snapshot("mwb", key.monday, "T")
        assert current is not None
        assert any(node.get("title") == "Healthy new image" for node in iter_nodes(current.nodes))
        original_media = {node["id"] for node in iter_nodes(original.nodes) if node["type"] == "media"}
        assert original_media <= {node["id"] for node in iter_nodes(current.nodes)}
        assert any(
            node.get("title") == "Healthy new image" for node in iter_nodes(current.canonical_nodes)
        )
        assert current.canonical_hash == ""
        assert current.source_checksum == original.source_checksum
        assert current.publication_dependencies == original.publication_dependencies
        assert service.state(key, "mwb").snapshot == current
    finally:
        service.shutdown(wait_ms=1000)


def test_cached_result_does_not_retire_generation_before_remote_merge(monkeypatch, tmp_path):
    service, publication, _store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.prepare_meeting(key, "wt")
        generation = int(publication.loads[-1]["generation"])
        wd = WeekData(
            monday=key.monday, language_code="T", request_generation=generation,
            wt_status="ready", wt_issue="20260400", wt_source_checksum="cached",
            wt_all_media=[MeetingMedia(multimedia_id=1, mime_type="image/jpeg", label="Cached")],
        )
        publication.wt_ready.emit(key.monday.isoformat(), wd)
        assert service.state(key, "wt").refreshing
        wd.wt_source_checksum = "remote"
        wd.wt_all_media[0].label = "Remote"
        publication.wt_ready.emit(key.monday.isoformat(), wd)
        state = service.state(key, "wt")
        assert state.snapshot is not None
        assert any(node.get("title") == "Remote" for node in iter_nodes(state.snapshot.nodes))
        assert state.snapshot.source_checksum == "remote"
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        assert not service.state(key, "wt").refreshing
    finally:
        service.shutdown(wait_ms=1000)


def test_automatic_download_prefetches_persisted_url_before_revalidation(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    store.save(
        "mwb:2026-05-25:T:20260500",
        [
            {
                "id": "official",
                "type": "media",
                "children": [],
                "meeting_generated": True,
                "meeting_source_key": "media:mwb:official",
                "resolved_url": "https://cdn.example/meeting.mp4",
                "media_ref": {"key_symbol": "mwbv", "track": 1},
            }
        ],
        "hash",
    )
    try:
        service.ensure_week(MeetingPreparationRequest(key=key, download_media=True))

        assert cache.prefetch_calls[0][0] == ["https://cdn.example/meeting.mp4"]
        assert len(publication.loads) == 1
        assert publication.loads[0]["content_request"].publications == {"mwb"}
        assert publication.loads[0]["content_request"].operation == MeetingContentOperation.CHECK
        assert publication.loads[0]["known_wt_issue"] == ""
        assert publication.loads[0]["content_request"].dependencies["mwb"][0]["checksum"] == ""
    finally:
        service.shutdown(wait_ms=1000)


def test_legacy_persisted_week_materializes_cache_to_seed_canonical_baseline(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    tree_key = "mwb:2026-05-25:T:20260500"
    store.save(
        tree_key,
        [
            {
                "id": "legacy-official",
                "type": "media",
                "children": [],
                "meeting_generated": True,
                "meeting_source_key": "media:mwb:legacy",
            }
        ],
        "legacy-hash",
        source_checksum="confirmed",
    )
    try:
        legacy = store.snapshot(tree_key)
        assert legacy is not None
        assert legacy.canonical_nodes == []

        service.prepare_meeting(key, "mwb", force=True)

        request = publication.loads[0]
        assert request["content_request"].publications == {"mwb"}
        assert request["content_request"].operation == MeetingContentOperation.UPDATE
        assert request["content_request"].dependencies["mwb"][0]["checksum"] == "confirmed"

        generation = int(request["generation"])
        publication.mwb_ready.emit(
            key.monday.isoformat(),
            _week_data(
                generation,
                with_media=True,
                source_checksum="confirmed",
            ),
        )

        reconciled = store.snapshot(tree_key)
        assert reconciled is not None
        assert reconciled.canonical_nodes
        restored_candidate = clone_nodes(reconciled.nodes)
        official_media = next(
            node
            for node in iter_nodes(restored_candidate)
            if node.get("type") == "media" and node.get("meeting_generated")
        )
        source_key = str(official_media["meeting_source_key"])
        for parent in iter_nodes(restored_candidate):
            children = parent.get("children", [])
            if official_media in children:
                children.remove(official_media)
                break
        assert canonical_tree_diff(
            reconciled.canonical_nodes,
            restored_candidate,
            {source_key},
        ).has_changes
    finally:
        service.shutdown(wait_ms=1000)


def test_persisted_week_skips_cached_materialization_and_passes_wt_issue(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    for pub_type, issue in (("mwb", "20260500"), ("wt", "20260400")):
        canonical = [
            {
                "id": f"{pub_type}-section",
                "type": "section",
                "title": "Section",
                "children": [],
                "meeting_generated": True,
                "meeting_source_key": f"section:{pub_type}:official",
            }
        ]
        store.save(
            f"{pub_type}:2026-05-25:T:{issue}",
            canonical,
            "hash",
            canonical_nodes=canonical,
        )
    try:
        service.check_updates(key)

        request = publication.loads[0]
        assert request["content_request"].operation == MeetingContentOperation.CHECK
        assert request["known_wt_issue"] == "20260400"
        assert set(request["content_request"].dependencies) == {"mwb", "wt"}
    finally:
        service.shutdown(wait_ms=1000)


def test_language_context_change_retires_obsolete_jobs(monkeypatch, tmp_path) -> None:
    service, _publication, _store, cache = _service(monkeypatch, tmp_path)
    old_key = MeetingPreparationKey(date(2026, 5, 25), "T")
    new_key = MeetingPreparationKey(date(2026, 5, 25), "E")
    try:
        service.ensure_week(MeetingPreparationRequest(key=old_key, download_media=True))
        service.ensure_week(MeetingPreparationRequest(key=new_key, download_media=True))

        service.cancel_other_language_contexts(JWMediaLanguageContext("E", "E", False))

        assert old_key not in service._jobs
        assert new_key in service._jobs
        assert cache.cancel_calls == [
            "meeting:2026-05-25:T:spoken:1",
        ]
    finally:
        service.shutdown(wait_ms=1000)


def test_repair_never_reconciles_publication_results_or_changes_the_saved_revision(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    tree_key = "mwb:2026-05-25:T:20260500"
    canonical = [
        {
            "id": "section-lac",
            "type": "section",
            "meeting_generated": True,
            "meeting_source_key": "section:mwb:lac",
            "children": [
                {
                    "id": "book",
                    "type": "subsection",
                    "meeting_generated": True,
                    "meeting_source_key": "subsection:wcg:book",
                    "children": [
                        {
                            "id": f"book-image-{index}",
                            "type": "media",
                            "meeting_generated": True,
                            "meeting_source_key": f"media:wcg:image:{index}",
                            "media_ref": {"file_path": f"C:/cache/wcg_T/x_0/image-{index}.jpg",
                                          "mime_type": "image/jpeg"},
                            "children": [],
                        }
                        for index in range(5)
                    ],
                }
            ],
        }
    ]
    original = store.save(
        tree_key,
        canonical,
        "confirmed-hash",
        canonical_nodes=canonical,
        source_checksum="confirmed-checksum",
    )
    published: list[MeetingTreeSnapshot] = []
    service.tree_changed.connect(lambda _key, _pub_type, snapshot: published.append(snapshot))
    monkeypatch.setattr(
        service._builder,
        "build_midweek",
        lambda week: [] if week.cbs_status == "loading" else canonical,
    )

    try:
        service.recover_missing_canonical_media(
            key,
            "mwb",
            "C:/cache/wcg_T/x_0/image-1.jpg",
        )
        service._flush_canonical_media_recoveries()
        generation = int(publication.loads[0]["generation"])
        partial = _week_data(
            generation,
            with_media=False,
            source_checksum="confirmed-checksum",
            cbs_status="loading",
        )

        publication.mwb_ready.emit(key.monday.isoformat(), partial)

        current = store.find_snapshot("mwb", key.monday, "T")
        assert current is not None
        assert current.revision == original.revision
        assert current.nodes == original.nodes
        assert current.canonical_nodes == original.canonical_nodes
        assert published == [original]

        partial.cbs_status = "ready"
        publication.cbs_ready.emit(key.monday.isoformat(), partial)

        completed = store.find_snapshot("mwb", key.monday, "T")
        assert completed is not None
        assert completed.revision == original.revision
        assert {node["id"] for node in iter_nodes(completed.nodes)} == {
            node["id"] for node in iter_nodes(original.nodes)
        }
        assert completed.canonical_nodes == original.canonical_nodes
        assert published == [original]
    finally:
        service.shutdown(wait_ms=1000)


def test_missing_canonical_media_recovery_materializes_only_the_affected_publication(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    missing_path = "C:/cache/jwpub/mwb_T/x_20260500/image.jpg"
    for pub_type, issue in (("mwb", "20260500"), ("wt", "20260400")):
        canonical = [
            {
                "id": f"{pub_type}-section",
                "type": "section",
                "meeting_generated": True,
                "meeting_source_key": f"section:{pub_type}:official",
                "children": [{"id": f"{pub_type}-image", "type": "media", "children": [],
                              "meeting_generated": True,
                              "meeting_source_key": f"media:{pub_type}:image",
                              "media_ref": {"file_path": missing_path,
                                            "mime_type": "image/jpeg"}}],
            }
        ]
        store.save(
            f"{pub_type}:{key.monday.isoformat()}:T:{issue}",
            canonical,
            "hash",
            canonical_nodes=canonical,
        )

    try:
        service.recover_missing_canonical_media(key, "mwb", missing_path)
        service._flush_canonical_media_recoveries()

        assert len(publication.loads) == 1
        request = publication.loads[0]["content_request"]
        assert request.operation == MeetingContentOperation.REPAIR
        assert request.publications == {"mwb"}
        assert len(request.repair_refs) == 1
    finally:
        service.shutdown(wait_ms=1000)


def test_recovery_waiting_on_unchanged_startup_runs_when_the_load_finishes(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    missing_path = "C:/cache/jwpub/mwb_T/x_20260500/image.jpg"
    for pub_type, issue in (("mwb", "20260500"), ("wt", "20260400")):
        canonical = [
            {
                "id": f"{pub_type}-section",
                "type": "section",
                "meeting_generated": True,
                "meeting_source_key": f"section:{pub_type}:official",
                "children": [{"id": f"{pub_type}-image", "type": "media", "children": [],
                              "meeting_generated": True,
                              "meeting_source_key": f"media:{pub_type}:image",
                              "media_ref": {"file_path": missing_path,
                                            "mime_type": "image/jpeg"}}],
            }
        ]
        store.save(
            f"{pub_type}:{key.monday.isoformat()}:T:{issue}",
            canonical,
            "hash",
            canonical_nodes=canonical,
        )

    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        service.recover_missing_canonical_media(key, "mwb", missing_path)
        service._flush_canonical_media_recoveries()

        assert len(publication.loads) == 1

        publication.context_load_finished.emit(
            key.monday.isoformat(),
            key.language_code,
            key.is_sign_language,
            generation,
        )

        assert len(publication.loads) == 2
        command = publication.loads[1]["content_request"]
        assert command.publications == {"mwb"}
        assert command.operation == MeetingContentOperation.REPAIR
    finally:
        service.shutdown(wait_ms=1000)


def test_missing_canonical_media_events_are_batched_into_one_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    first = "C:/cache/jwpub/wcg_T/x_0/images/first.jpg"
    second = "C:/cache/jwpub/wcg_T/x_0/images/second.jpg"
    images = [
        {"id": str(index), "type": "media", "children": [], "meeting_generated": True,
         "meeting_source_key": f"media:image:{index}",
         "media_ref": {"mime_type": "image/jpeg", "file_path": path}}
        for index, path in enumerate((first, second))
    ]
    store.save("mwb:2026-05-25:T:20260500", images, "baseline", canonical_nodes=images)
    try:
        service.recover_missing_canonical_media(key, "mwb", first)
        service.recover_missing_canonical_media(key, "mwb", second)

        assert publication.loads == []

        _spin_until(lambda: len(publication.loads) == 1)

        assert len(publication.loads) == 1
        request = publication.loads[0]["content_request"]
        assert request.publications == {"mwb"}
        assert request.operation == MeetingContentOperation.REPAIR
        assert len(publication.loads) == 1
    finally:
        service.shutdown(wait_ms=1000)


def test_shutdown_ignores_late_publication_callback(monkeypatch, tmp_path) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    service.ensure_week(MeetingPreparationRequest(key=key))
    generation = int(publication.loads[0]["generation"])

    service.shutdown(wait_ms=1000)
    service._accept_publication("mwb", _week_data(generation, with_media=True))

    assert store.find_snapshot("mwb", key.monday, key.language_code) is None


def test_late_generation_cannot_overwrite_forced_refresh(monkeypatch, tmp_path) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        first_generation = int(publication.loads[0]["generation"])
        first = _week_data(first_generation, with_media=False)
        publication.mwb_ready.emit(key.monday.isoformat(), first)
        publication.wt_ready.emit(
            key.monday.isoformat(),
            WeekData(
                monday=key.monday,
                language_code="T",
                request_generation=first_generation,
                wt_status="ready",
                wt_issue="20260500",
            ),
        )
        publication.context_load_finished.emit(
            key.monday.isoformat(), "T", False, first_generation
        )
        service.ensure_week(MeetingPreparationRequest(key=key, force_refresh=True))
        second_generation = int(publication.loads[-1]["generation"])
        assert second_generation > first_generation

        stale = _week_data(first_generation, with_media=False)
        stale.mwb_date_label = "Stale"
        publication.mwb_ready.emit(key.monday.isoformat(), stale)

        snapshot = store.find_snapshot("mwb", key.monday, "T")
        assert snapshot is not None
        assert snapshot.overview.title == "May 25-31"
    finally:
        service.shutdown(wait_ms=1000)


def test_pending_force_refresh_runs_when_final_publication_fails(
    monkeypatch,
    tmp_path,
) -> None:
    service, publication, _store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        service.ensure_week(MeetingPreparationRequest(key=key, force_refresh=True))

        publication.context_error.emit(
            key.monday.isoformat(),
            "mwb",
            "network error",
            key.language_code,
            key.is_sign_language,
            generation,
        )
        publication.context_error.emit(
            key.monday.isoformat(),
            "wt",
            "network error",
            key.language_code,
            key.is_sign_language,
            generation,
        )

        assert len(publication.loads) == 2
        assert publication.loads[-1]["force"] is True
        assert int(publication.loads[-1]["generation"]) > generation
    finally:
        service.shutdown(wait_ms=1000)


def test_resolution_patch_preserves_concurrent_manual_insertion(monkeypatch, tmp_path) -> None:
    service, publication, store, cache = _service(
        monkeypatch,
        tmp_path,
        _DeferredResolver,
    )
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key, download_media=True))
        generation = int(publication.loads[0]["generation"])
        publication.mwb_ready.emit(key.monday.isoformat(), _week_data(generation, with_media=True))
        _spin_until(lambda: bool(_DeferredResolver.latest and _DeferredResolver.latest.request_id))

        snapshot = store.find_snapshot("mwb", key.monday, "T")
        assert snapshot is not None
        edited_nodes = clone_nodes(snapshot.nodes)
        edited_nodes.append(
            {
                "id": "manual",
                "type": "media",
                "title": "Manual",
                "media_type": "video",
                "media_ref": {"file_path": "manual.mp4"},
                "children": [],
                "meeting_generated": False,
            }
        )
        store.save(
            snapshot.tree_key,
            edited_nodes,
            snapshot.canonical_hash,
            snapshot.deleted_source_keys,
            snapshot.linked_folder_files,
            snapshot.meeting_folder_imports,
            snapshot.overview,
        )

        assert _DeferredResolver.latest is not None
        _DeferredResolver.latest.complete()
        _spin_until(lambda: bool(cache.prefetch_calls))

        updated = store.find_snapshot("mwb", key.monday, "T")
        assert updated is not None
        assert any(node.get("id") == "manual" for node in iter_nodes(updated.nodes))
        assert any(node.get("resolved_url") for node in iter_nodes(updated.nodes))
    finally:
        service.shutdown(wait_ms=1000)


def test_resolved_snapshot_is_available_after_offline_restart(monkeypatch, tmp_path) -> None:
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.ensure_week(MeetingPreparationRequest(key=key))
        generation = int(publication.loads[0]["generation"])
        publication.mwb_ready.emit(key.monday.isoformat(), _week_data(generation, with_media=True))
        _spin_until(
            lambda: (
                (snapshot := store.find_snapshot("mwb", key.monday, "T")) is not None
                and any(node.get("resolved_url") for node in iter_nodes(snapshot.nodes))
            )
        )
    finally:
        service.shutdown(wait_ms=1000)

    reloaded = MeetingTreeStore(tmp_path / "meeting_trees.json")
    snapshot = reloaded.find_snapshot("mwb", key.monday, "T")
    assert snapshot is not None
    assert any(
        node.get("resolved_url") == "https://cdn.example/meeting.mp4"
        for node in iter_nodes(snapshot.nodes)
    )


def test_selected_initial_preparation_and_force_retry_never_include_other_meeting(
    monkeypatch, tmp_path
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.prepare_meeting(key, "wt", force=True)
        command = publication.loads[0]["content_request"]
        assert command.publications == {"wt"}
        assert command.operation == MeetingContentOperation.UPDATE
        assert store.snapshots_for_week(key.monday, "T") == {}
    finally:
        service.shutdown(wait_ms=1000)


def test_maintenance_only_checks_saved_meeting_and_observes_book_changes(monkeypatch, tmp_path):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=False, auto_recover_images=False)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    original = store.save(
        "mwb:2026-05-25:T:20260500",
        [],
        "baseline",
        source_checksum="parent-old",
        publication_dependencies=[
            {"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "parent-old"},
            {"pub": "book", "language": "T", "issue": "0", "checksum": "book-old"},
        ],
    )
    try:
        service.prepare_meeting(key, "mwb", automatic=True)
        command = publication.loads[0]["content_request"]
        assert command.operation == MeetingContentOperation.CHECK
        assert command.publications == {"mwb"}
        generation = publication.loads[0]["generation"]
        service._on_content_checked(
            (key.monday, "T", False, generation),
            "mwb",
            {"mwb:T:20260500": "parent-old", "book:T:0": "book-new"},
        )
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        state = service.state(key, "mwb")
        assert state.update_available
        store.observe_publications(state.snapshot.tree_key, {})
        assert service.state(key, "mwb").update_available
        assert state.snapshot.source_checksum == "parent-old"
        assert state.snapshot.publication_dependencies == original.publication_dependencies
        assert state.snapshot.nodes == original.nodes
        assert len(publication.loads) == 1
        assert service.snapshot(key, "wt") is None
    finally:
        service.shutdown(wait_ms=1000)


def test_missing_images_with_automatic_repair_disabled_require_explicit_repair(
    monkeypatch, tmp_path
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=False, auto_recover_images=False)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    image = {
        "type": "media",
        "id": "image",
        "meeting_generated": True,
        "media_ref": {"file_path": str(tmp_path / "missing.jpg"), "mime_type": "image/jpeg"},
        "children": [],
    }
    original = store.save("mwb:2026-05-25:T:20260500", [image], "baseline", canonical_nodes=[image])
    try:
        service.maintain_prepared(key)
        generation = publication.loads[0]["generation"]
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        assert len(publication.loads) == 1
        assert service.state(key, "mwb").repair_required
        service.repair_meeting(key, "mwb")
        assert publication.loads[-1]["content_request"].operation == MeetingContentOperation.REPAIR
        assert service.snapshot(key, "mwb").nodes == original.nodes
    finally:
        service.shutdown(wait_ms=1000)


def test_cancelling_automatic_preparation_preserves_shared_manual_request(monkeypatch, tmp_path):
    service, publication, _store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    try:
        service.prepare_meeting(key, "mwb", automatic=True)
        service.prepare_meeting(key, "mwb")
        service.cancel_automatic_downloads()
        assert service._jobs[key].active
        assert service._jobs[key].origin == MeetingRequestOrigin.MANUAL
        assert len(publication.loads) == 1
        assert cache.cancel_calls == []
    finally:
        service.shutdown(wait_ms=1000)


def test_empty_week_maintenance_does_not_prepare_any_meeting(monkeypatch, tmp_path):
    service, publication, _store, _cache = _service(monkeypatch, tmp_path)
    try:
        service.maintain_prepared(MeetingPreparationKey(date(2026, 5, 25), "T"))
        assert publication.loads == []
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("auto_recover", [False, True])
def test_prepared_maintenance_update_and_repair_flags_are_independent(
    monkeypatch, tmp_path, auto_update, auto_recover
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(
        auto_update=auto_update, auto_recover_images=auto_recover
    )
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    image = {
        "type": "media",
        "id": "image",
        "meeting_generated": True,
        "media_ref": {"file_path": str(tmp_path / "missing.jpg"), "mime_type": "image/jpeg"},
        "children": [],
    }
    original = store.save(
        "mwb:2026-05-25:T:20260500",
        [image],
        "baseline",
        canonical_nodes=[image],
        source_checksum="old",
        publication_dependencies=[
            {"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "old"}
        ],
    )
    try:
        service.maintain_prepared(key)
        generation = publication.loads[0]["generation"]
        service._on_content_checked(
            (key.monday, "T", False, generation), "mwb", {"mwb:T:20260500": "new"}
        )
        for index in range(4):
            if index >= len(publication.loads):
                break
            generation = publication.loads[index]["generation"]
            publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        operations = [load["content_request"].operation for load in publication.loads]
        assert operations.count(MeetingContentOperation.CHECK) == 1
        assert (MeetingContentOperation.REPAIR in operations) is auto_recover
        assert (MeetingContentOperation.UPDATE in operations) is auto_update
        assert all(load["content_request"].publications == {"mwb"} for load in publication.loads)
        assert service.snapshot(key, "mwb").nodes == original.nodes
        assert service.snapshot(key, "mwb").source_checksum == "old"
    finally:
        service.shutdown(wait_ms=1000)


def test_expiration_cancels_old_jobs_before_removing_snapshots(monkeypatch, tmp_path):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    old_key = MeetingPreparationKey(date(2026, 5, 4), "T")
    current_key = MeetingPreparationKey(date(2026, 5, 25), "T")
    store.save("mwb:2026-05-04:T:20260500", [], "old")
    store.save("mwb:2026-05-25:T:20260500", [], "current")
    try:
        service.check_updates(old_key)
        old_generation = publication.loads[0]["generation"]
        assert service.prune_expired() == 1
        assert service.snapshot(old_key, "mwb") is None
        assert service.snapshot(current_key, "mwb") is not None
        service._accept_publication("mwb", _week_data(old_generation, with_media=False))
        assert service.snapshot(old_key, "mwb") is None
    finally:
        service.shutdown(wait_ms=1000)


def test_mixed_week_initial_preparation_never_updates_prepared_half(monkeypatch, tmp_path):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=False, auto_recover_images=False)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    original = store.save("mwb:2026-05-25:T:20260500", [], "baseline", source_checksum="old")
    try:
        service.maintain_prepared(key)
        service.ensure_week(
            MeetingPreparationRequest(key=key, origin=MeetingRequestOrigin.AUTOMATIC)
        )
        first_generation = publication.loads[0]["generation"]
        service._on_content_checked(
            (key.monday, "T", False, first_generation), "mwb", {"mwb:T:20260500": "new"}
        )
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, first_generation)
        assert len(publication.loads) == 2
        first, second = (load["content_request"] for load in publication.loads)
        assert first.operation == MeetingContentOperation.CHECK
        assert first.publications == {"mwb"}
        assert second.operation == MeetingContentOperation.PREPARE
        assert second.publications == {"wt"}
        assert service.snapshot(key, "mwb").source_checksum == original.source_checksum
        assert service.state(key, "mwb").update_available
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize(
    "active_context",
    [JWMediaLanguageContext("E", "E", False), JWMediaLanguageContext("T", "E", True)],
)
def test_language_change_discards_obsolete_commands_without_replaying_updates(
    monkeypatch, tmp_path, active_context
):
    service, publication, _store, _cache = _service(monkeypatch, tmp_path)
    old_key = MeetingPreparationKey(date(2026, 5, 25), "T")
    active_key = MeetingPreparationKey.from_context(old_key.monday, active_context)
    orphan_key = MeetingPreparationKey(date(2026, 5, 18), "T")
    initial = MeetingPreparationRequest(key=old_key, publications=frozenset({"mwb"}))
    try:
        service.ensure_week(initial)
        service.prepare_meeting(old_key, "mwb", force=True)
        service.ensure_week(
            MeetingPreparationRequest(key=active_key, publications=frozenset({"mwb"}))
        )
        service.prepare_meeting(active_key, "mwb", force=True)
        service._pending_commands[orphan_key] = [
            MeetingPreparationRequest(key=orphan_key, operation=MeetingContentOperation.UPDATE)
        ]

        service.cancel_other_language_contexts(active_context)

        assert old_key not in service._pending_commands
        assert orphan_key not in service._pending_commands
        active_generation = service._jobs[active_key].generation
        publication.context_load_finished.emit(
            active_key.monday.isoformat(),
            active_key.language_code,
            active_key.is_sign_language,
            active_generation,
        )
        assert publication.loads[-1]["content_request"].operation == MeetingContentOperation.UPDATE
        assert publication.loads[-1]["language_code"] == active_key.language_code
        assert publication.loads[-1]["is_sign_language"] == active_key.is_sign_language

        service.ensure_week(initial)
        generation = service._jobs[old_key].generation
        publication.context_load_finished.emit(old_key.monday.isoformat(), "T", False, generation)

        assert [
            load["content_request"].operation
            for load in publication.loads
            if load["monday"] == old_key.monday
            and load["language_code"] == "T"
            and not load["is_sign_language"]
        ] == [MeetingContentOperation.PREPARE, MeetingContentOperation.PREPARE]
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("entrypoint", ["prepare_meeting", "prepared_week", "mixed_week"])
def test_explicit_saved_meeting_download_survives_automatic_cancellation(
    monkeypatch, tmp_path, automatic, entrypoint
):
    service, publication, store, cache = _service(monkeypatch, tmp_path, _DeferredResolver)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    nodes = [
        {
            "id": "official",
            "type": "media",
            "children": [],
            "meeting_generated": True,
            "meeting_source_key": "media:mwb:official",
            "resolved_url": "https://cdn.example/meeting.mp4",
            "media_ref": {"key_symbol": "mwbv", "track": 1},
        }
    ]
    store.save("mwb:2026-05-25:T:20260500", nodes, "hash", canonical_nodes=nodes)
    try:
        if entrypoint == "prepare_meeting":
            service.prepare_meeting(key, "mwb", download_media=True, automatic=automatic)
        else:
            service.ensure_week(
                MeetingPreparationRequest(
                    key=key,
                    download_media=True,
                    publications=frozenset(
                        {"mwb", "wt"} if entrypoint == "mixed_week" else {"mwb"}
                    ),
                    origin=MeetingRequestOrigin.AUTOMATIC
                    if automatic
                    else MeetingRequestOrigin.MANUAL,
                )
            )
        assert cache.prefetch_calls
        batch_id = cache.prefetch_calls[0][1]

        service.cancel_automatic_downloads()

        assert cache.cancel_calls == ([batch_id] if automatic else [])
        assert service._jobs[key].download_media is (not automatic)
        if entrypoint == "mixed_week":
            generation = service._jobs[key].generation
            publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
            assert [load["content_request"].operation for load in publication.loads] == (
                [MeetingContentOperation.CHECK]
                if automatic
                else [MeetingContentOperation.CHECK, MeetingContentOperation.PREPARE]
            )
            service.cancel_automatic_downloads()
            assert cache.cancel_calls == ([batch_id] if automatic else [])
    finally:
        service.shutdown(wait_ms=1000)


@pytest.fixture
def missing_image_meeting(monkeypatch, tmp_path):
    service, publication, store, cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    image = {
        "type": "media",
        "id": "image",
        "meeting_generated": True,
        "media_ref": {"file_path": str(tmp_path / "missing.jpg"), "mime_type": "image/jpeg"},
        "children": [],
    }
    store.save(
        "mwb:2026-05-25:T:20260500", [image], "baseline", canonical_nodes=[image]
    )
    try:
        yield service, publication, key, cache
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("physical_missing", [False, True])
def test_canonical_image_repair_rebinds_empty_visible_sources_without_update(
    monkeypatch, tmp_path, physical_missing
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    path = tmp_path / "image.jpg"
    canonical = {
        "id": "image", "type": "media", "children": [], "meeting_generated": True,
        "meeting_source_key": "media:image",
        "media_ref": {"mime_type": "image/jpeg", "file_path": str(path), "asset_hash": "hash",
                      "publication_source": {"pub": "mwb", "language": "T", "issue": "20260500",
                                             "checksum": "applied", "member": "image.jpg"}},
    }
    visible = clone_nodes([canonical])[0]
    visible["media_ref"] = {"mime_type": "image/jpeg", "file_path": ""}
    original = store.save("mwb:2026-05-25:T:20260500", [visible], "confirmed",
                          canonical_nodes=[canonical], source_checksum="applied")
    if not physical_missing:
        path.write_bytes(b"image")
    changed = []
    service.tree_changed.connect(lambda *_args: changed.append(_args[-1]))
    try:
        assert service.state(key, "mwb").repair_required
        service.repair_meeting(key, "mwb")
        if physical_missing:
            assert len(publication.loads) == 1
            command = publication.loads[0]["content_request"]
            assert command.operation == MeetingContentOperation.REPAIR
            assert len(command.repair_refs) == 1
            assert command.repair_refs[0]["file_path"] == str(path)
            path.write_bytes(b"restored")
            publication.context_load_finished.emit(
                key.monday.isoformat(), "T", False, publication.loads[0]["generation"]
            )
        else:
            assert not publication.loads
        assert len(changed) == 1
        current = service.snapshot(key, "mwb")
        assert current.nodes[0]["media_ref"]["file_path"] == str(path)
        assert current.source_checksum == original.source_checksum
        assert current.canonical_nodes == original.canonical_nodes
        assert not service.state(key, "mwb").repair_required
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("auto_recover", [False, True])
def test_maintenance_rebinds_available_image_without_network_repair(monkeypatch, tmp_path, auto_recover):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=False, auto_recover_images=auto_recover)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    path = tmp_path / "available-later.jpg"
    canonical = {"id": "image", "type": "media", "children": [], "meeting_generated": True,
                 "meeting_source_key": "media:image",
                 "media_ref": {"mime_type": "image/jpeg", "file_path": str(path)}}
    visible = clone_nodes([canonical])[0]
    visible["media_ref"]["file_path"] = ""
    store.save("mwb:2026-05-25:T:20260500", [visible], "confirmed", canonical_nodes=[canonical])
    path.write_bytes(b"available")
    try:
        service.maintain_prepared(key)
        assert service.snapshot(key, "mwb").nodes[0]["media_ref"]["file_path"] == str(path)
        assert all(load["content_request"].operation == MeetingContentOperation.CHECK
                   for load in publication.loads)
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("auto_update", [False, True])
def test_unknown_empty_image_sources_reuse_existing_typed_update_fallback(
    monkeypatch, tmp_path, auto_update
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=auto_update)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    images = [
        {"id": f"image-{index}", "type": "media", "children": [], "meeting_generated": True,
         "meeting_source_key": f"media:image:{index}",
         "media_ref": {"mime_type": "image/jpeg", "file_path": ""}}
        for index in range(2)
    ]
    store.save("mwb:2026-05-25:T:20260500", images, "baseline", canonical_nodes=images)
    try:
        assert service.state(key, "mwb").repair_required
        service.repair_meeting(key, "mwb")
        command = publication.loads[0]
        assert len(command["content_request"].repair_refs) == 2
        publication.context_error.emit(
            key.monday.isoformat(), "mwb", MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
            "T", False, command["generation"],
        )
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False,
                                               command["generation"])
        assert len(publication.loads) == (2 if auto_update else 1)
        assert service.state(key, "mwb").repair_requires_update
        if auto_update:
            assert publication.loads[-1]["content_request"].operation == MeetingContentOperation.UPDATE
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("remote", [False, True])
def test_blank_image_file_path_with_usable_resolution_does_not_trigger_repair(
    monkeypatch, tmp_path, remote
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")
    image = {"id": "image", "type": "media", "children": [], "meeting_generated": True,
             "meeting_source_key": "media:image", "media_ref": {"mime_type": "image/jpeg"},
             "resolved_url": "https://example.test/image.jpg" if remote else str(path)}
    store.save("mwb:2026-05-25:T:20260500", [image], "baseline", canonical_nodes=[image])
    try:
        assert not service.state(key, "mwb").repair_required
        service.repair_meeting(key, "mwb")
        assert not publication.loads
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("known_canonical_source", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_usable_visible_image_does_not_hide_known_missing_canonical_asset(
    monkeypatch, tmp_path, known_canonical_source, remote
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    path = tmp_path / "visible.jpg"
    path.write_bytes(b"image")
    canonical = {"id": "image", "type": "media", "children": [], "meeting_generated": True,
                 "meeting_source_key": "media:image",
                 "media_ref": {"mime_type": "image/jpeg", "file_path": ""}}
    if known_canonical_source:
        canonical["media_ref"].update(
            file_path=str(tmp_path / "missing-canonical.jpg"), asset_hash="a" * 64,
            publication_source={"pub": "mwb", "language": "T", "issue": "20260500",
                                "checksum": "applied", "member": "image.jpg"},
        )
    visible = clone_nodes([canonical])[0]
    visible["resolved_url"] = "https://example.test/image.jpg" if remote else str(path)
    visible["media_ref"]["file_path"] = ""
    store.save("mwb:2026-05-25:T:20260500", [visible], "confirmed", canonical_nodes=[canonical])
    try:
        assert service.state(key, "mwb").repair_required is known_canonical_source
        service.repair_meeting(key, "mwb")
        assert len(publication.loads) == (1 if known_canonical_source else 0)
        if known_canonical_source:
            refs = publication.loads[0]["content_request"].repair_refs
            assert len(refs) == 1
            assert refs[0]["file_path"] == canonical["media_ref"]["file_path"]
    finally:
        service.shutdown(wait_ms=1000)


def test_repair_finish_respects_sync_enabled_after_worker_dispatch(monkeypatch, tmp_path):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    path = tmp_path / "image.jpg"
    canonical = {"id": "image", "type": "media", "children": [], "meeting_generated": True,
                 "meeting_source_key": "media:image",
                 "media_ref": {"mime_type": "image/jpeg", "file_path": str(path)}}
    visible = clone_nodes([canonical])[0]
    visible["media_ref"]["file_path"] = ""
    original = store.save("mwb:2026-05-25:T:20260500", [visible], "baseline",
                          canonical_nodes=[canonical])
    changed = []
    service.tree_changed.connect(lambda *_args: changed.append(_args[-1]))
    try:
        service.repair_meeting(key, "mwb")
        active = store.save(original.tree_key, [visible], "baseline", linked_sync={"enabled": True})
        path.write_bytes(b"restored")
        publication.context_load_finished.emit(
            key.monday.isoformat(), "T", False, publication.loads[0]["generation"]
        )
        current = store.snapshot(original.tree_key)
        assert current.revision == active.revision
        assert current.nodes[0]["media_ref"]["file_path"] == ""
        assert current.linked_sync == {"enabled": True}
        assert changed == []
    finally:
        service.shutdown(wait_ms=1000)


def test_canonical_recovery_with_automatic_repair_disabled_keeps_physical_work_manual(
    missing_image_meeting
):
    service, publication, key, _cache = missing_image_meeting
    service._policy = MeetingContentPolicy(auto_recover_images=False)
    service.recover_missing_canonical_media(key, "mwb", "")
    service._flush_canonical_media_recoveries()
    assert not publication.loads
    assert service.state(key, "mwb").repair_required
    service.repair_meeting(key, "mwb")
    assert publication.loads[0]["content_request"].operation == MeetingContentOperation.REPAIR


@pytest.mark.parametrize("unrelated_repair_fails", [False, True])
def test_canonical_recovery_delivers_repaired_source_while_other_image_is_missing(
    monkeypatch, tmp_path, unrelated_repair_fails
):
    service, publication, store, _cache = _service(monkeypatch, tmp_path)
    service._policy = MeetingContentPolicy(auto_update=False)
    key = MeetingPreparationKey(date(2026, 5, 25), "T")
    first, second = (tmp_path / name for name in ("first.jpg", "second.jpg"))
    canonical = [
        {"id": name, "type": "media", "children": [], "meeting_generated": True,
         "meeting_source_key": f"media:{name}",
         "media_ref": {"mime_type": "image/jpeg", "file_path": str(path)}}
        for name, path in (("first", first), ("second", second))
    ]
    visible = clone_nodes(canonical)
    visible[0]["media_ref"]["file_path"] = ""
    original = store.save("mwb:2026-05-25:T:20260500", visible, "confirmed",
                          canonical_nodes=canonical, source_checksum="applied")
    first.write_bytes(b"available first image")
    # The controller's pending write persists the normalized source but keeps its old local nodes.
    persisted = store.save(original.tree_key, visible, original.canonical_hash)
    assert persisted.nodes[0]["media_ref"]["file_path"] == str(first)
    published = []
    service.tree_changed.connect(lambda *_args: published.append(_args[-1]))
    try:
        service.recover_missing_canonical_media(key, "mwb", "")
        service._flush_canonical_media_recoveries()

        assert len(published) == 1
        assert published[0].revision == persisted.revision
        assert published[0].nodes[0]["media_ref"]["file_path"] == str(first)
        assert service.state(key, "mwb").repair_required
        assert store.snapshot(original.tree_key).revision == persisted.revision
        command = publication.loads[0]
        assert len(command["content_request"].repair_refs) == 1
        assert command["content_request"].repair_refs[0]["file_path"] == str(second)
        if unrelated_repair_fails:
            publication.context_error.emit(
                key.monday.isoformat(), "mwb", MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
                "T", False, command["generation"],
            )
        else:
            second.write_bytes(b"restored second image")
        publication.context_load_finished.emit(
            key.monday.isoformat(), "T", False, command["generation"]
        )
        assert len(published) == 1
        assert published[0].source_checksum == "applied"
        assert len(publication.loads) == 1
    finally:
        service.shutdown(wait_ms=1000)


@pytest.mark.parametrize("failure", list(MeetingContentError))
@pytest.mark.parametrize("auto_update", [False, True])
def test_unrecoverable_image_uses_update_policy_after_repair_finishes(
    missing_image_meeting, failure, auto_update
):
    service, publication, key, cache = missing_image_meeting
    # Initial automatic downloads may be disabled; saved-content policy still applies.
    service._policy = MeetingContentPolicy(auto_update=auto_update, auto_recover_images=False)
    service.repair_meeting(key, "mwb")
    generation = publication.loads[-1]["generation"]
    for _ in range(2):
        publication.context_error.emit(key.monday.isoformat(), "mwb", failure, "T", False, generation)
    assert len(publication.loads) == 1
    assert service.state(key, "mwb").repair_requires_update
    assert not service.state(key, "mwb").update_available

    publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)

    assert len(publication.loads) == (2 if auto_update else 1)
    if auto_update:
        update = publication.loads[-1]
        assert update["content_request"].operation == MeetingContentOperation.UPDATE
        assert update["content_request"].publications == {"mwb"}
        assert not update["force"]
        assert service._jobs[key].origin == MeetingRequestOrigin.MAINTENANCE
        service.cancel_automatic_downloads()
        assert service._jobs[key].active
        # Neither duplicate completion nor an UPDATE failure can recursively rebuild.
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
        next_generation = update["generation"]
        publication.context_error.emit(
            key.monday.isoformat(), "mwb", failure, "T", False, next_generation
        )
        publication.context_load_finished.emit(key.monday.isoformat(), "T", False, next_generation)
        assert len(publication.loads) == 2
    assert cache.prefetch_calls == []


@pytest.mark.parametrize("combined", [False, True])
def test_repair_fallback_coalesces_existing_manual_update(missing_image_meeting, combined):
    service, publication, key, _cache = missing_image_meeting
    service.repair_meeting(key, "mwb")
    generation = publication.loads[-1]["generation"]
    service.ensure_week(
        MeetingPreparationRequest(
            key=key,
            operation=MeetingContentOperation.UPDATE,
            publications=frozenset({"mwb", "wt"} if combined else {"mwb"}),
            force_refresh=True,
        )
    )
    publication.context_error.emit(
        key.monday.isoformat(), "mwb", MeetingContentError.IMAGE_REVISION_UNAVAILABLE,
        "T", False, generation,
    )
    assert len(publication.loads) == 1
    publication.context_load_finished.emit(key.monday.isoformat(), "T", False, generation)
    update = publication.loads[-1]
    publication.context_load_finished.emit(key.monday.isoformat(), "T", False, update["generation"])
    assert len(publication.loads) == 2
    assert update["force"]
    assert service._jobs[key].origin == MeetingRequestOrigin.MANUAL


@pytest.mark.parametrize("retirement", ["language", "sign", "shutdown"])
def test_retired_repair_cannot_schedule_update(missing_image_meeting, retirement):
    service, publication, key, _cache = missing_image_meeting
    service.repair_meeting(key, "mwb")
    generation = publication.loads[-1]["generation"]
    publication.context_error.emit(
        key.monday.isoformat(), "mwb", MeetingContentError.IMAGE_REVISION_UNAVAILABLE,
        "T", False, generation,
    )
    if retirement == "shutdown":
        service.shutdown(wait_ms=1000)
    else:
        service.cancel_other_language_contexts(
            JWMediaLanguageContext("E", "E", False)
            if retirement == "language" else JWMediaLanguageContext("T", "T", True)
        )
    service._on_load_finished(key.monday.isoformat(), "T", False, generation)
    service._on_error(
        key.monday.isoformat(), "mwb", MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
        "T", False, generation,
    )
    assert len(publication.loads) == 1
