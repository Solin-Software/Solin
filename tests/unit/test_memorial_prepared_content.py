from __future__ import annotations

from datetime import date
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from PySide6.QtCore import QCoreApplication

import solin.core.meetings.memorial_worker as worker_module
from solin.core.jw.memorial_publication import MemorialJwpubInfo
from solin.core.meetings.content_policy import (
    MeetingContentError,
    MeetingContentOperation,
    MeetingContentPolicy,
)
from solin.core.meetings.jwpub_cache import JwpubChecksumStore
from solin.core.meetings.memorial import MemorialService
from solin.core.meetings.memorial_content import MemorialPublicationContent
from solin.core.meetings.memorial_worker import MemorialWorker
from solin.core.media.publication_assets import PublicationAssetStore
from solin.core.meetings.models import MeetingMedia, MemorialData
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_store import MeetingTreeOverview, MeetingTreeStore


_APP = QCoreApplication.instance() or QCoreApplication([])


@pytest.mark.parametrize("first_suffix", [".jpg", ".jpeg"])
def test_memorial_repairs_deleted_shared_image_at_its_saved_path(
    tmp_path, monkeypatch, first_suffix
):
    assets = PublicationAssetStore(tmp_path / "assets")
    first = tmp_path / f"first{first_suffix}"
    first.write_bytes(b"original image")
    destination = assets.materialize(first)
    root = tmp_path / "extracted"
    root.mkdir()
    member = root / "other.jpg"
    member.write_bytes(first.read_bytes())
    saved = MemorialData(
        year=2026,
        videos=[
            MeetingMedia(
                mime_type="image/jpeg",
                file_path=destination,
                asset_hash=hashlib.sha256(first.read_bytes()).hexdigest(),
                publication_source={"member": "other.jpg"},
            )
        ],
    )
    assert assets.remove(destination)
    worker = MemorialWorker(
        tmp_path / "jwpub",
        JwpubChecksumStore(tmp_path / "checksums.json"),
        asset_store=assets,
    )
    monkeypatch.setattr(worker._cache, "extract", lambda *_: root)
    try:
        assert worker._repair_saved(saved, "mi26", "T", "0", "", "saved", explicit=True)
        assert Path(destination).read_bytes() == first.read_bytes()
    finally:
        worker.release_assets()


def _saved_service(tmp_path, *, content_policy=None):
    store = MeetingTreeStore(tmp_path / "meetings.json")
    media = MeetingMedia(
        multimedia_id=1,
        mime_type="image/jpeg",
        file_path=str(tmp_path / "missing.jpg"),
        publication_source={
            "pub": "mi26",
            "language": "T",
            "issue": "0",
            "checksum": "old",
            "member": "image.jpg",
        },
        asset_hash="a" * 64,
    )
    data = MemorialData(year=2026, memorial_date=date(2026, 4, 2), videos=[media])
    builder = MeetingTreeBuilder()
    canonical = builder.build_memorial(data)
    original = store.save(
        "memorial:2026-04-02:T:2026",
        canonical,
        builder.canonical_hash(canonical),
        canonical_nodes=canonical,
        source_checksum="old",
        overview=MeetingTreeOverview(cover_bytes=b"cover"),
    )
    service = MemorialService(
        tmp_path / "jwpub",
        JwpubChecksumStore(tmp_path / "checksums.json"),
        tree_store=store,
        content_policy=content_policy
        or MeetingContentPolicy(auto_update=False, auto_recover_images=False),
    )
    service._year = 2026
    return service, store, original


def test_saved_memorial_restores_cover_and_provenance_without_network(monkeypatch, tmp_path):
    service, store, original = _saved_service(tmp_path)
    monkeypatch.setattr(
        worker_module,
        "resolve_memorial_jwpub",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("Saved restoration cannot query remote data")
        ),
    )
    try:
        assert service.has_prepared_content()
        restored = service.load_saved()
        assert restored.cover_bytes == b"cover"
        assert restored.videos[0].asset_hash == "a" * 64
        assert restored.videos[0].publication_source["checksum"] == "old"
        assert service.repair_required
        assert not service.update_available
        assert store.snapshot(original.tree_key).revision == original.revision
    finally:
        service.shutdown(wait_ms=1000)


def test_memorial_remote_observation_and_saved_repair_do_not_change_applied_snapshot(tmp_path):
    service, store, original = _saved_service(tmp_path)
    try:
        restored = service.load_saved()
        service._on_content_checked("T", 2026, "new")
        assert service.update_available
        service._on_saved_ready(restored)
        current = store.snapshot(original.tree_key)
        assert current.source_checksum == "old"
        assert current.nodes == original.nodes
        assert current.revision == original.revision
        assert service.update_available
        service._on_content_checked("E", 2026, "ignored")
        assert store.snapshot(original.tree_key).observed_publication_checksums == {
            "mi26:T:0": "new"
        }
    finally:
        service.shutdown(wait_ms=1000)


@pytest.fixture
def recorded_service(tmp_path, monkeypatch):
    service, store, original = _saved_service(
        tmp_path,
        content_policy=MeetingContentPolicy(auto_recover_images=False),
    )
    commands = []
    events = []
    service._sig_load.disconnect(service._worker.load_memorial)

    def record(year, options):
        commands.append((year, options))
        events.append("load")

    service._sig_load.connect(record)
    monkeypatch.setattr(service._worker, "release_assets", lambda: events.append("release"))
    service.content_settled.connect(lambda: events.append("settled"))
    try:
        yield service, store, original, commands, events
    finally:
        service.shutdown(wait_ms=1000)


_REPAIR_ERRORS = [
    MeetingContentError.IMAGE_REVISION_UNAVAILABLE,
    MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
]


@pytest.mark.parametrize("error", _REPAIR_ERRORS)
@pytest.mark.parametrize("explicit_repair", [False, True])
@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("observed_checksum", ["", "old"])
def test_unrecoverable_memorial_queues_update_only_after_repair_finishes(
    recorded_service, error, explicit_repair, auto_update, observed_checksum
):
    service, store, original, commands, events = recorded_service
    service.content_policy = MeetingContentPolicy(
        auto_update=auto_update, auto_recover_images=False
    )
    if explicit_repair:
        service.repair_saved()
    else:
        service.load()
    saved = service.get_data()
    service._on_content_checked("T", 2026, observed_checksum)
    assert not service.update_available

    service._on_error(error)
    service._on_error(error)
    service._on_saved_ready(saved)

    assert len(commands) == 1
    assert events == ["load"]
    assert service.repair_requires_update
    assert store.snapshot(original.tree_key).source_checksum == "old"
    service._on_finished()

    assert events[:3] == ["load", "release", "settled"]
    assert len(commands) == (2 if auto_update else 1)
    if not auto_update:
        service.load(force=True)
    assert commands[1][1]["force"] is True
    assert commands[1][1]["saved"] is saved


@pytest.mark.parametrize("update_error", [*_REPAIR_ERRORS, "network unavailable"])
def test_failed_memorial_fallback_update_does_not_retry_itself(recorded_service, update_error):
    service, store, original, commands, _ = recorded_service
    service.repair_saved()
    service._on_error(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
    service._on_finished()
    assert len(commands) == 2

    service._on_error(update_error)
    service._on_done(MemorialData(year=2026, language_code="T", status="error"))
    service._on_finished()

    assert len(commands) == 2
    assert not service._loading
    assert service.repair_requires_update
    assert service.get_data().status == "ready"
    assert store.snapshot(original.tree_key).nodes == original.nodes


@pytest.mark.parametrize("return_to_original_language", [False, True])
@pytest.mark.parametrize("error_before_switch", [False, True])
def test_memorial_discards_old_context_repair_failures_and_queued_updates(
    recorded_service, return_to_original_language, error_before_switch
):
    service, store, original, commands, _ = recorded_service
    service.repair_saved()
    stale = service.get_data()
    if error_before_switch:
        service._on_error(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
    service.set_lang("E")
    if return_to_original_language:
        service.set_lang("T")

    service._on_error(MeetingContentError.LEGACY_REVISION_UNVERIFIABLE)
    service._on_content_checked("T", 2026, "stale")
    service._on_saved_ready(stale)
    service._on_done(stale)
    service._on_finished()

    assert len(commands) == 1
    assert service.get_data() is None
    assert not service.repair_requires_update
    assert not service.update_available
    assert store.snapshot(original.tree_key).revision == original.revision


def test_memorial_old_context_failure_preserves_new_context_manual_update(recorded_service):
    service, _, _, commands, _ = recorded_service
    service.repair_saved()
    service._on_error(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
    service.set_lang("E")
    service.load(force=True)
    service._on_error(MeetingContentError.LEGACY_REVISION_UNVERIFIABLE)
    service._on_finished()

    assert len(commands) == 2
    assert commands[1][1]["force"] is True
    assert commands[1][1]["saved"] is None
    assert service.get_lang() == "E"
    assert not service.repair_requires_update


def test_memorial_shutdown_drops_queued_fallback_update(recorded_service):
    service, _, _, commands, _ = recorded_service
    service.repair_saved()
    service._on_error(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
    service.shutdown(wait_ms=1000)
    service._on_error(MeetingContentError.LEGACY_REVISION_UNVERIFIABLE)
    service._on_finished()

    assert len(commands) == 1


@pytest.mark.parametrize("repair_before_update", [False, True])
@pytest.mark.parametrize("remote_checksum_known", [False, True])
@pytest.mark.parametrize("previous_checksum", ["", "previous-revision"])
def test_memorial_download_records_archive_identity_before_fallback_update(
    tmp_path, monkeypatch, repair_before_update, remote_checksum_known, previous_checksum
):
    inner = BytesIO()
    with ZipFile(inner, "w") as archive:
        archive.writestr("publication.db", b"parser fixture")
        archive.writestr("image.jpg", b"new image revision")
    outer = BytesIO()
    with ZipFile(outer, "w") as archive:
        archive.writestr("contents", inner.getvalue())
    archive_bytes = outer.getvalue()
    actual_checksum = hashlib.md5(archive_bytes).hexdigest()
    remote_checksum = actual_checksum if remote_checksum_known else ""
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    checksums.save("mi26", "T", "0", previous_checksum)
    assets = PublicationAssetStore(tmp_path / "assets")
    worker = MemorialWorker(tmp_path / "jwpub", checksums, asset_store=assets)
    old_image_hash = hashlib.sha256(b"old image revision").hexdigest()
    saved = MemorialData(
        year=2026,
        language_code="T",
        source_checksum="applied-revision",
        videos=[
            MeetingMedia(
                mime_type="image/jpeg",
                file_path=str(assets.root / f"{old_image_hash}.jpg"),
                publication_source={"member": "image.jpg"},
                asset_hash=old_image_hash,
            )
        ],
    )
    downloads = []
    results = []
    errors = []
    finished = []
    worker.memorial_done.connect(results.append)
    worker.error.connect(errors.append)
    worker.finished.connect(lambda: finished.append(True))
    monkeypatch.setattr(
        worker_module,
        "resolve_memorial_jwpub",
        lambda *_: MemorialJwpubInfo("https://example.test/memorial.jwpub", checksum=remote_checksum),
    )

    def download(_url, destination, **options):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive_bytes)
        downloads.append(options["expected_checksum"])

    monkeypatch.setattr(worker_module, "download_jwpub_archive", download)
    monkeypatch.setattr(
        worker_module,
        "read_memorial_publication_content",
        lambda root: MemorialPublicationContent(
            cover_bytes=None,
            media_items=[MeetingMedia(mime_type="image/jpeg", file_path=str(root / "image.jpg"))],
        ),
    )
    try:
        if repair_before_update:
            worker.load_memorial(
                2026, {"saved": saved, "operation": MeetingContentOperation.REPAIR}
            )
            assert errors == [MeetingContentError.IMAGE_REVISION_UNAVAILABLE]
            assert not results
        worker.load_memorial(2026, {"saved": saved, "force": True})

        assert len(finished) == (2 if repair_before_update else 1)
        assert downloads == [remote_checksum]
        assert len(results) == 1
        result = results[0]
        assert result.status == "ready"
        assert checksums.get("mi26", "T", "0") == actual_checksum
        assert result.source_checksum == actual_checksum
        assert result.publication_dependencies[0]["checksum"] == actual_checksum
        image = result.videos[0]
        assert image.publication_source["checksum"] == actual_checksum
        assert image.asset_hash == hashlib.sha256(b"new image revision").hexdigest()
        assert assets.owns(image.file_path)
        assert Path(image.file_path).read_bytes() == b"new image revision"
    finally:
        worker.release_assets()


def test_memorial_unknown_download_identity_does_not_reuse_previous_checksum(tmp_path, monkeypatch):
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    checksums.save("mi26", "T", "0", "previous-revision")
    worker = MemorialWorker(tmp_path / "jwpub", checksums)
    saved = MemorialData(year=2026, source_checksum="previous-revision")
    results = []
    errors = []
    worker.memorial_done.connect(results.append)
    worker.error.connect(errors.append)
    monkeypatch.setattr(
        worker_module,
        "resolve_memorial_jwpub",
        lambda *_: MemorialJwpubInfo("https://example.test/memorial.jwpub"),
    )
    monkeypatch.setattr(
        worker_module,
        "download_jwpub_archive",
        lambda _url, destination, **_: destination.write_bytes(b"downloaded publication"),
    )
    monkeypatch.setattr(worker_module, "local_jwpub_checksum", lambda *_: "")

    worker.load_memorial(2026, {"saved": saved, "force": True})

    assert errors
    assert not any(result.status == "ready" for result in results)
    assert checksums.get("mi26", "T", "0") == ""
    assert saved.source_checksum == "previous-revision"
