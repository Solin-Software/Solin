"""Global ownership and fail-closed publication migration contracts."""

from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import io
import os
from pathlib import Path
import threading
import zipfile

import pytest

from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from solin.core.ingest.sync.journal import JournalReplica, ReplicaSnapshot
from solin.core.media.publication_assets import PublicationAssetStore
from solin.core.meetings.asset_lifecycle import MeetingAssetLifecycle
from solin.core.meetings.content_policy import (
    MeetingContentOperation,
    MeetingContentPolicy,
    PublicationContentRequest,
)
from solin.core.meetings.jwpub_cache import JwpubCache, JwpubChecksumStore
from solin.core.meetings.meeting_weeks import meeting_week_bounds
from solin.core.meetings.tree_builder import canonical_media_source_hash
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.meetings.tree_types import source_hash
from solin.core.meetings.publication_worker import JwpubWorker
from solin.core.playlists.cleanup import flush_images_dir
from solin.core.playlists.storage import PlaylistRepository, PlaylistStoragePaths
from solin.core.scenes.model import (
    DELIVERY_BUSES,
    NO_SIGNAL_SOURCE_ID,
    AutomationMap,
    ColorSourceConfig,
    ImageSourceConfig,
    OutputRoute,
    SceneDefinition,
    SceneDocument,
    SourceDefinition,
    SourceKind,
)
from solin.core.storage.json_repository import JsonFileRepository


TODAY = date(2026, 11, 3)
TREE_KEY = "mwb:2026-11-02:T:20261100"


@pytest.fixture
def runtime(tmp_path):
    return RuntimePaths.from_roots(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")


def _profile(runtime, name="inactive"):
    paths = ProfilePaths.from_roots(
        data_dir=runtime.data_dir,
        cache_dir=runtime.cache_dir,
        profile_id=name,
    )
    paths.ensure_dirs()
    return paths


def _asset(store, tmp_path, name="orphan"):
    source = tmp_path / f"{name}.jpg"
    source.write_bytes(name.encode())
    return store.materialize(source)


def _node(path, *, node_id="image"):
    return {
        "id": node_id,
        "type": "media",
        "media_type": "image",
        "title": "Image",
        "media_ref": {"file_path": str(path), "mime_type": "image/jpeg", "multimedia_id": 7},
        "resolved_url": str(path),
        "children": [],
        "meeting_generated": True,
        "meeting_source_key": "media:mwb:mm7",
        "meeting_source_hash": "legacy",
        "image_framing": {"version": 1, "zoom": 2.0, "norm_x": 0.1, "norm_y": 0.0},
    }


def _publication(runtime, *, payload=b"original image", archive_payload=None):
    root = runtime.jwpub_cache_dir / "mwb_T"
    source = root / "x_20261100" / "images" / "illustration.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as inner:
        inner.writestr("publication.db", b"synthetic")
        inner.writestr(
            "images/illustration.jpg", payload if archive_payload is None else archive_payload
        )
    archive = root / "mwb_T_20261100.jwpub"
    with zipfile.ZipFile(archive, "w") as outer:
        outer.writestr("contents", contents.getvalue())
    return source, archive


def _lifecycle(runtime, *, store=None, **kwargs):
    return MeetingAssetLifecycle(
        runtime,
        store or PublicationAssetStore(runtime.publication_assets_dir),
        kwargs.pop("policy", MeetingContentPolicy()),
        today=TODAY,
        **kwargs,
    )


def _scene_document(asset_id):
    source = SourceDefinition(
        id="image-source",
        kind=SourceKind.IMAGE,
        name="Image",
        configuration=ImageSourceConfig(asset_id),
    )
    return SceneDocument(
        document_id="scene-document",
        revision=0,
        sources=(
            source,
            SourceDefinition(
                id=NO_SIGNAL_SOURCE_ID,
                kind=SourceKind.COLOR,
                name="Background",
                configuration=ColorSourceConfig(),
            ),
        ),
        scenes=(SceneDefinition(id="scene", name="Scene"),),
        outputs=tuple(OutputRoute(bus_id=bus) for bus in DELIVERY_BUSES),
        automation=tuple(AutomationMap(bus_id=bus, assignments=()) for bus in DELIVERY_BUSES),
    )


def test_migrates_every_profile_and_recovery_view_before_gc(runtime, tmp_path):
    _profile(runtime, "active")
    paths = _profile(runtime)
    source, archive = _publication(runtime)
    original_bytes = source.read_bytes()
    node = _node(source)
    PlaylistRepository(paths.playlists_file).save_strict(
        [{"id": "playlist", "items": [{"id": "item", "url": str(source), "type": "image"}]}]
    )
    store = MeetingTreeStore(paths.meeting_trees_file)
    store.save(
        TREE_KEY,
        [{"type": "section", "children": [copy.deepcopy(node)]}],
        "legacy",
        canonical_nodes=[copy.deepcopy(node)],
        hidden_canonical_media={node["meeting_source_key"]: copy.deepcopy(node)},
    )
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)

    lifecycle = _lifecycle(runtime, store=assets, profile_catalog=lambda: ["active"])
    assert lifecycle.maintain() == [orphan]

    record = store.load_all_strict()["trees"][TREE_KEY]
    views = [
        record["nodes"][0]["children"][0],
        record["canonical_nodes"][0],
        record["hidden_canonical_media"][node["meeting_source_key"]],
    ]
    destinations = {view["media_ref"]["file_path"] for view in views}
    assert len(destinations) == 1
    destination = destinations.pop()
    assert assets.owns(destination)
    assert Path(destination).read_bytes() == original_bytes
    for view in views:
        assert view["id"] == node["id"]
        assert view["meeting_source_key"] == node["meeting_source_key"]
        assert view["image_framing"] == node["image_framing"]
        assert view["resolved_url"] == destination
        assert view["media_ref"]["asset_hash"] == hashlib.sha256(original_bytes).hexdigest()
        assert view["media_ref"]["publication_source"]["member"] == "images/illustration.jpg"
        assert view["meeting_source_hash"] == canonical_media_source_hash(view)
    assert record["last_canonical_hash"] == source_hash(record["canonical_nodes"])
    assert (
        PlaylistRepository(paths.playlists_file).load_strict()[0]["items"][0]["url"] == destination
    )
    assert not source.exists() and archive.exists()
    assert not lifecycle.protected_publication_sources()
    assert lifecycle.maintain() == []


def test_migration_rewrites_local_aliases_but_preserves_sync_baseline(runtime):
    paths = _profile(runtime)
    source, _ = _publication(runtime)
    node = _node(source)
    node["media_ref"]["file_path"] = ""
    node["source_url"] = str(source)
    snapshot = ReplicaSnapshot(entities={"image": copy.deepcopy(node)}).to_dict()
    store = MeetingTreeStore(paths.meeting_trees_file)
    store.save(
        TREE_KEY,
        [node],
        "hash",
        linked_folder_files={str(source): node["id"]},
        meeting_folder_imports={str(source): {"path": str(source)}},
        linked_sync={"snapshot": snapshot},
    )

    lifecycle = _lifecycle(runtime)
    lifecycle.maintain()

    record = store.load_all_strict()["trees"][TREE_KEY]
    migrated = record["nodes"][0]
    destination = migrated["resolved_url"]
    assert migrated["source_url"] == destination
    assert migrated["media_ref"]["asset_hash"] == Path(destination).stem
    assert set(record["linked_folder_files"]) == {destination}
    assert record["meeting_folder_imports"][destination]["path"] == destination
    assert record["linked_sync"]["snapshot"] == snapshot
    assert str(source) in lifecycle.protected_publication_sources()


@pytest.mark.parametrize("owner", ["playlists", "meetings", "hidden", "scenes", "scene_catalog"])
def test_invalid_inactive_owner_blocks_writes_and_collection(runtime, tmp_path, owner, monkeypatch):
    active = _profile(runtime, "active")
    inactive = _profile(runtime)
    old_key = "mwb:2020-01-06:T:20200100"
    MeetingTreeStore(active.meeting_trees_file).save(old_key, [], "old")
    before = active.meeting_trees_file.read_bytes()
    if owner == "playlists":
        JsonFileRepository(inactive.playlists_file).write({"version": 999, "playlists": []})
    elif owner == "meetings":
        inactive.meeting_trees_file.write_text("{broken", encoding="utf-8")
    elif owner == "hidden":
        JsonFileRepository(inactive.meeting_trees_file).write(
            {"version": 6, "trees": {TREE_KEY: {"nodes": [], "hidden_canonical_media": []}}}
        )
    elif owner == "scenes":
        inactive.scenes_file.write_text("{broken", encoding="utf-8")
    else:
        JsonFileRepository(inactive.scene_profiles_catalog_file).write({"schema_version": 999})
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)
    lifecycle = _lifecycle(runtime, store=assets)

    def unexpected_prune(*args, **kwargs):
        pytest.fail("invalid profile owner must prevent archive pruning")

    monkeypatch.setattr(JwpubCache, "prune", unexpected_prune)

    with pytest.raises((ValueError, RuntimeError)):
        lifecycle.maintain()

    assert Path(orphan).exists()
    assert active.meeting_trees_file.read_bytes() == before
    assert str(runtime.jwpub_cache_dir) in lifecycle.protected_publication_sources()


def test_unreadable_profile_blocks_collection(runtime, tmp_path, monkeypatch):
    paths = _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)
    original = PlaylistRepository.load_strict

    def load(repository):
        if repository.path == paths.playlists_file:
            raise PermissionError(repository.path)
        return original(repository)

    monkeypatch.setattr(PlaylistRepository, "load_strict", load)
    with pytest.raises(PermissionError):
        _lifecycle(runtime, store=assets).maintain()
    assert Path(orphan).exists()


def test_inactive_expiry_uses_week_bounds_and_preserves_boundary(runtime, tmp_path):
    paths = _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    expired = _asset(assets, tmp_path, "expired")
    boundary = _asset(assets, tmp_path, "boundary")
    cutoff, _ = meeting_week_bounds(TODAY)
    old_key = f"mwb:{cutoff - timedelta(weeks=1)}:T:20261100"
    boundary_key = f"mwb:{cutoff}:T:20261100"
    repository = MeetingTreeStore(paths.meeting_trees_file)
    repository.save(old_key, [_node(expired)], "old")
    repository.save(boundary_key, [_node(boundary)], "boundary")

    assert _lifecycle(runtime, store=assets).maintain() == [expired]
    assert set(repository.load_all_strict()["trees"]) == {boundary_key}
    assert Path(boundary).exists()


def test_runtime_collection_never_prunes_or_rewrites_repositories(runtime, tmp_path):
    paths = _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    kept = _asset(assets, tmp_path, "expired-but-still-persisted")
    MeetingTreeStore(paths.meeting_trees_file).save(
        "mwb:2020-01-06:T:20200100", [_node(kept)], "hash"
    )
    before = paths.meeting_trees_file.read_bytes()
    assert _lifecycle(runtime, store=assets).collect() == []
    assert paths.meeting_trees_file.read_bytes() == before
    assert Path(kept).exists()


@pytest.mark.parametrize("reason", ["missing_archive", "different_archive"])
def test_unknown_provenance_stays_raw_and_protected(runtime, reason):
    paths = _profile(runtime)
    source, archive = _publication(runtime, archive_payload=b"new archive image")
    if reason == "missing_archive":
        archive.unlink()
    MeetingTreeStore(paths.meeting_trees_file).save(TREE_KEY, [_node(source)], "hash")
    lifecycle = _lifecycle(runtime)
    lifecycle.maintain()
    record = MeetingTreeStore(paths.meeting_trees_file).load_all_strict()["trees"][TREE_KEY]
    assert record["nodes"][0]["media_ref"]["file_path"] == str(source)
    assert str(source) in lifecycle.protected_publication_sources()


def test_missing_legacy_image_does_not_block_other_profiles(runtime, tmp_path):
    broken = _profile(runtime, "a-missing")
    ready = _profile(runtime, "b-ready")
    source, _ = _publication(runtime)
    missing = source.with_name("missing.jpg")
    MeetingTreeStore(broken.meeting_trees_file).save(TREE_KEY, [_node(missing)], "hash")
    MeetingTreeStore(ready.meeting_trees_file).save(TREE_KEY, [_node(source)], "hash")
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)
    lifecycle = _lifecycle(runtime, store=assets)

    assert lifecycle.maintain() == [orphan]

    unresolved = MeetingTreeStore(broken.meeting_trees_file).load_all_strict()["trees"][TREE_KEY]
    migrated = MeetingTreeStore(ready.meeting_trees_file).load_all_strict()["trees"][TREE_KEY]
    assert unresolved["nodes"][0]["media_ref"]["file_path"] == str(missing)
    assert "publication_source" not in unresolved["nodes"][0]["media_ref"]
    assert assets.owns(migrated["nodes"][0]["media_ref"]["file_path"])
    assert str(missing) in lifecycle.protected_publication_sources()
    assert str(runtime.jwpub_cache_dir) not in lifecycle.protected_publication_sources()


def test_missing_cached_member_stays_unresolved_until_revision_proven_repair(runtime):
    paths = _profile(runtime)
    source, archive = _publication(runtime)
    source.unlink()
    os.utime(archive, (0, 0))
    MeetingTreeStore(paths.meeting_trees_file).save(TREE_KEY, [_node(source)], "hash")
    lifecycle = _lifecycle(runtime)

    assert lifecycle.maintain() == []

    record = MeetingTreeStore(paths.meeting_trees_file).load_all_strict()["trees"][TREE_KEY]
    assert not source.exists()
    assert archive.exists()
    assert record["nodes"][0]["media_ref"]["file_path"] == str(source)
    assert "publication_source" not in record["nodes"][0]["media_ref"]
    assert str(source) in lifecycle.protected_publication_sources()
    assert str(runtime.jwpub_cache_dir) not in lifecycle.protected_publication_sources()


@pytest.mark.parametrize("proof", ["matching", "different", "absent"])
def test_legacy_repair_requires_saved_archive_proof_before_next_startup_migration(
    runtime,
    monkeypatch,
    proof,
):
    paths = _profile(runtime)
    source, archive = _publication(runtime)
    original_bytes = source.read_bytes()
    checksum = hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest()
    saved_checksum = (
        checksum if proof == "matching" else ("older-revision" if proof == "different" else "")
    )
    source.unlink()
    node = _node(source)
    store = MeetingTreeStore(paths.meeting_trees_file)
    store.save(TREE_KEY, [node], "hash", canonical_nodes=[node], source_checksum=saved_checksum)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    lifecycle = _lifecycle(runtime, store=assets)
    lifecycle.maintain()
    errors = []
    changed = []
    worker = JwpubWorker(
        runtime.jwpub_cache_dir,
        JwpubChecksumStore(runtime.jwpub_cache_dir / "checksums.json"),
        asset_store=assets,
        protected_publication_sources=lifecycle.protected_publication_sources,
    )
    worker.error.connect(lambda *args: errors.append(args))
    worker.asset_changed.connect(changed.append)

    def unexpected_network(*args, **kwargs):
        pytest.fail("Legacy repair must not fetch an unverified archive revision")

    monkeypatch.setattr(
        "solin.core.meetings.publication_worker.resolve_jwpub_archive",
        unexpected_network,
    )
    worker.load_week(
        date(2026, 11, 2),
        content_request=PublicationContentRequest(
            operation=MeetingContentOperation.REPAIR,
            publications=frozenset({"mwb"}),
            dependencies={
                "mwb": [
                    {
                        "pub": "mwb",
                        "language": "T",
                        "issue": "20261100",
                        "checksum": saved_checksum,
                    }
                ]
            },
            repair_refs=(node["media_ref"],),
        ),
    )

    if proof == "matching":
        assert source.read_bytes() == original_bytes
        assert changed and not errors
        lifecycle.maintain()
        record = store.load_all_strict()["trees"][TREE_KEY]
        migrated = record["nodes"][0]
        assert assets.owns(migrated["media_ref"]["file_path"])
        assert migrated["media_ref"]["publication_source"]["checksum"] == saved_checksum
        assert migrated["id"] == node["id"]
        assert migrated["image_framing"] == node["image_framing"]
    else:
        assert errors and not changed
        assert not source.exists()
        assert archive.exists()
        assert store.load_all_strict()["trees"][TREE_KEY]["nodes"][0] == node


@pytest.mark.parametrize("keep_images", [True, False])
def test_startup_prunes_idle_archives_with_automatic_content_disabled(
    runtime, tmp_path, keep_images
):
    _profile(runtime)
    source, archive = _publication(runtime)
    os.utime(archive, (0, 0))
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)
    lifecycle = _lifecycle(
        runtime,
        store=assets,
        policy=MeetingContentPolicy(
            keep_images_for_manual_cleanup=keep_images,
            auto_recover_images=False,
            auto_update=False,
        ),
    )

    assert lifecycle.maintain() == ([] if keep_images else [orphan])

    assert not archive.exists()
    assert not source.exists()
    assert Path(orphan).exists() is keep_images


def test_archive_prune_runs_after_asset_lock_is_released(runtime, monkeypatch):
    _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    acquired = threading.Event()
    completed = threading.Event()

    def access_assets():
        try:
            with assets.lease([]):
                acquired.set()
        finally:
            completed.set()

    def prune(*args, **kwargs):
        worker = threading.Thread(target=access_assets, daemon=True)
        worker.start()
        try:
            assert acquired.wait(timeout=2), "archive pruning held the asset lock"
        finally:
            worker.join(timeout=2)

    monkeypatch.setattr(JwpubCache, "prune", prune)

    assert _lifecycle(runtime, store=assets).collect() == []
    assert completed.is_set()


def test_background_collection_prunes_idle_archive_without_publication_requests(
    runtime, monkeypatch, request
):
    _profile(runtime)
    source, archive = _publication(runtime)
    os.utime(archive, (0, 0))
    lifecycle = _lifecycle(runtime)
    request.addfinalizer(lifecycle.shutdown)
    pruned = threading.Event()
    original = JwpubCache.prune

    def prune(cache, *args, **kwargs):
        result = original(cache, *args, **kwargs)
        pruned.set()
        return result

    monkeypatch.setattr(JwpubCache, "prune", prune)
    lifecycle.request_collection()
    assert pruned.wait(timeout=2)
    lifecycle.shutdown()

    assert not source.exists()
    assert not archive.exists()


def test_migration_failure_prevents_completed_asset_gc(runtime, tmp_path, monkeypatch):
    paths = _profile(runtime)
    source, _ = _publication(runtime)
    MeetingTreeStore(paths.meeting_trees_file).save(TREE_KEY, [_node(source)], "hash")
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)

    def fail(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(assets, "materialize", fail)
    lifecycle = _lifecycle(runtime, store=assets)
    with pytest.raises(OSError):
        lifecycle.maintain()
    assert source.exists() and Path(orphan).exists()
    assert str(runtime.jwpub_cache_dir) in lifecycle.protected_publication_sources()


def test_manual_cleanup_policy_retains_images_but_reclaims_deleted_metadata(runtime, tmp_path):
    _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)

    deleted = _asset(assets, tmp_path, name="deleted")
    assert assets.remove(deleted)
    lifecycle = _lifecycle(
        runtime, store=assets, policy=MeetingContentPolicy(keep_images_for_manual_cleanup=True)
    )
    assert lifecycle.maintain() == []
    assert Path(orphan).exists()
    assert not (assets.root / f"{Path(deleted).stem}.json").exists()


def test_background_cache_budget_prefers_current_meeting_book(runtime, monkeypatch):
    paths = _profile(runtime)
    MeetingTreeStore(paths.meeting_trees_file).save(
        TREE_KEY,
        [],
        "canonical",
        canonical_nodes=[],
        publication_dependencies=[
            {"pub": "book", "language": "T", "issue": "0", "checksum": "saved"}
        ],
    )
    cache = JwpubCache(runtime.jwpub_cache_dir)
    book = cache.jwpub_path("book", "T", "0")
    unrelated = cache.jwpub_path("unused", "T", "0")
    for archive in (book, unrelated):
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"x" * 512)
    timestamp = unrelated.stat().st_mtime
    os.utime(book, (timestamp - 10, timestamp - 10))
    prune = JwpubCache.prune

    def bounded(cache, *args, **kwargs):
        return prune(cache, *args, **kwargs, max_bytes=512)

    monkeypatch.setattr(JwpubCache, "prune", bounded)
    _lifecycle(runtime).collect()
    assert book.exists()
    assert not unrelated.exists()


def test_active_provider_and_store_leases_protect_unsaved_assets(runtime, tmp_path):
    _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    active = _asset(assets, tmp_path, "active")
    leased = _asset(assets, tmp_path, "leased")
    external = tmp_path / "imported.jpg"
    external.write_bytes(b"user-owned image")
    lifecycle = _lifecycle(
        runtime, store=assets, active_paths_provider=lambda: {active, str(external)}
    )
    with assets.lease([leased]):
        assert lifecycle.collect() == []
    assert lifecycle.collect() == [leased]
    assert external.exists() and Path(active).exists()


@pytest.mark.parametrize("identifier_kind", ["digest", "filename"])
def test_scene_recovery_document_owns_global_asset(runtime, tmp_path, identifier_kind):
    paths = _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    kept = _asset(assets, tmp_path, "scene")
    document = _scene_document(
        assets.asset_id(kept) if identifier_kind == "digest" else Path(kept).name
    )
    JsonFileRepository(paths.scene_profiles_dir / "inactive.backup.json").write(
        document.to_record()
    )
    assert _lifecycle(runtime, store=assets).maintain() == []
    assert Path(kept).exists()


@pytest.mark.parametrize("identifier", ["abc.png", "ed39b01f-a07c-45da-824a-5c343b58284e"])
def test_imported_scene_identity_does_not_block_global_gc(runtime, tmp_path, identifier):
    paths = _profile(runtime)
    imported = paths.images_dir / identifier
    imported.write_bytes(b"scene import")
    JsonFileRepository(paths.scenes_file).write(_scene_document(identifier).to_record())
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)

    assert _lifecycle(runtime, store=assets).maintain() == [orphan]
    flush_images_dir(
        PlaylistStoragePaths(paths.playlists_file, paths.pending_deletions_file),
        MeetingTreeStore(paths.meeting_trees_file),
        paths,
    )
    assert imported.exists()


def test_pending_journal_owns_assets_and_history_is_not_rewritten(runtime, tmp_path):
    paths = _profile(runtime)
    source, _ = _publication(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    pending_asset = _asset(assets, tmp_path, "pending")
    replica = JournalReplica(
        tmp_path / "linked", "meeting", state_dir=runtime.data_dir / "linked_sync"
    )
    snapshot = replica.stage(
        ReplicaSnapshot(), {"pending": _node(pending_asset), "raw": _node(source)}
    )
    MeetingTreeStore(paths.meeting_trees_file).save(
        TREE_KEY,
        [_node(source)],
        "hash",
        linked_sync={"folder": str(replica.folder), "snapshot": snapshot.to_dict()},
    )
    operation_bytes = {path: path.read_bytes() for path in replica.outbox_dir.glob("*.json")}
    lifecycle = _lifecycle(runtime, store=assets)
    lifecycle.maintain()
    assert all(path.read_bytes() == payload for path, payload in operation_bytes.items())
    record = MeetingTreeStore(paths.meeting_trees_file).load_all_strict()["trees"][TREE_KEY]
    assert record["linked_sync"]["snapshot"] == snapshot.to_dict()
    assert assets.owns(record["nodes"][0]["media_ref"]["file_path"])
    assert Path(pending_asset).exists()
    assert str(source) in lifecycle.protected_publication_sources()


def test_corrupt_pending_journal_fails_closed(runtime, tmp_path):
    _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    orphan = _asset(assets, tmp_path)
    path = runtime.data_dir / "linked_sync" / "replicas" / "unknown" / "outbox" / "operation.json"
    JsonFileRepository(path).write({"version": 999, "namespace": "meeting"})
    with pytest.raises(ValueError):
        _lifecycle(runtime, store=assets).maintain()
    assert Path(orphan).exists()


def test_runtime_expiry_respects_registered_cached_owners(runtime, tmp_path):
    active = _profile(runtime, "active")
    inactive = _profile(runtime)
    assets = PublicationAssetStore(runtime.publication_assets_dir)
    active_asset = _asset(assets, tmp_path, "active-expired")
    inactive_asset = _asset(assets, tmp_path, "inactive-expired")
    old_key = "mwb:2020-01-06:T:20200100"
    active_store = MeetingTreeStore(active.meeting_trees_file)
    active_store.save(old_key, [_node(active_asset)], "old")
    MeetingTreeStore(inactive.meeting_trees_file).save(old_key, [_node(inactive_asset)], "old")
    lifecycle = _lifecycle(runtime, store=assets)
    assert lifecycle.prune_inactive() == 0  # registration is an explicit integration contract
    release = lifecycle.register_profile_owner("active")
    try:
        assert lifecycle.prune_inactive() == 1
        assert old_key in active_store.load_all_strict()["trees"]
        assert lifecycle.collect() == [inactive_asset]
        with pytest.raises(RuntimeError):
            lifecycle.maintain()
    finally:
        release()
        release()
    assert lifecycle.prune_inactive() == 1
    assert lifecycle.collect() == [active_asset]


def test_collection_requests_coalesce_and_replay_latest_commit(runtime, monkeypatch, request):
    lifecycle = _lifecycle(runtime)
    request.addfinalizer(lifecycle.shutdown)
    first_started = threading.Event()
    release_first = threading.Event()
    rerun_finished = threading.Event()
    request.addfinalizer(release_first.set)
    calls = []

    def collect():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            rerun_finished.set()
        return []

    monkeypatch.setattr(lifecycle, "collect", collect)
    lifecycle.request_collection()
    assert first_started.wait(timeout=2)
    lifecycle.request_collection()
    lifecycle.request_collection()
    release_first.set()
    assert rerun_finished.wait(timeout=2)
    lifecycle.shutdown()
    lifecycle.request_collection()
    assert len(calls) == 2
    assert set(calls).isdisjoint({threading.get_ident()})


def test_async_failure_protects_cache_and_does_not_lose_pending_request(
    runtime, monkeypatch, request, caplog
):
    lifecycle = _lifecycle(runtime)
    request.addfinalizer(lifecycle.shutdown)
    first_started = threading.Event()
    release_first = threading.Event()
    rerun_finished = threading.Event()
    request.addfinalizer(release_first.set)
    calls = []

    def collect():
        calls.append(True)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            raise OSError("repository unavailable")
        rerun_finished.set()
        return []

    monkeypatch.setattr(lifecycle, "collect", collect)
    lifecycle.request_collection()
    assert first_started.wait(timeout=2)
    lifecycle.request_collection()
    release_first.set()
    assert rerun_finished.wait(timeout=2)
    lifecycle.shutdown()
    assert len(calls) == 2
    assert str(runtime.jwpub_cache_dir) in lifecycle.protected_publication_sources()
    assert any(record.exc_info for record in caplog.records)
