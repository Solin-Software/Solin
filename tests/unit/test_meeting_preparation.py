from __future__ import annotations

import time
from datetime import date

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot

import solin.core.meetings.preparation as preparation_module
from solin.core.meetings.canonical_restore import canonical_tree_diff
from solin.core.meetings.models import MeetingMedia, WeekData
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

    def __init__(self) -> None:
        super().__init__()
        self.loads: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def load_week(self, monday, force=False, **context) -> None:
        self.loads.append({"monday": monday, "force": force, **context})

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
        assert service._jobs[key].download_media is False
        assert cache.cancel_calls == [service._jobs[key].batch_id]
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
        assert snapshot.canonical_nodes == []
        assert snapshot.source_checksum == ""

        partial.cbs_status = "ready"
        publication.cbs_ready.emit(key.monday.isoformat(), partial)

        confirmed = store.find_snapshot("mwb", key.monday, "T")
        assert confirmed is not None
        assert confirmed.canonical_nodes
        assert confirmed.source_checksum == "confirmed"
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
        assert publication.loads[0]["materialize_cached_publications"] == frozenset({"mwb", "wt"})
        assert publication.loads[0]["known_wt_issue"] == ""
        assert publication.loads[0]["persisted_source_checksums"] == {"mwb": ""}
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

        service.ensure_week(MeetingPreparationRequest(key=key))

        request = publication.loads[0]
        assert request["materialize_cached_publications"] == frozenset({"mwb", "wt"})
        assert request["persisted_source_checksums"] == {"mwb": "confirmed"}

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
        service.ensure_week(MeetingPreparationRequest(key=key))

        request = publication.loads[0]
        assert request["materialize_cached_publications"] == frozenset()
        assert request["known_wt_issue"] == "20260400"
        assert request["persisted_source_checksums"] == {"mwb": "", "wt": ""}
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
            "meeting:2026-05-25:T:spoken",
        ]
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
