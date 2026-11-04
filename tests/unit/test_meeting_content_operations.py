from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import solin.core.meetings.publication_worker as worker_module
from solin.core.jw.publication_links import JwpubMediaInfo
from solin.core.media.publication_assets import PublicationAssetStore
from solin.core.meetings.content_policy import (
    MeetingContentOperation,
    MeetingContentPolicy,
    PublicationContentRequest,
)
from solin.core.meetings.jwpub_cache import JwpubChecksumStore
from solin.core.meetings.models import MeetingMedia, MeetingPublicationRef
from solin.core.meetings.publication_content import (
    MwbPublicationContent,
    WatchtowerPublicationContent,
)
from solin.core.meetings.publication_worker import JwpubWorker
from solin.core.meetings.preparation import MeetingPreparationKey, MeetingPreparationService
from solin.core.meetings.tree_builder import MeetingTreeBuilder
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.meetings.tree_types import iter_nodes


MONDAY = date(2026, 5, 25)


def _archive(path: Path, image: bytes = b"original") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as inner:
        inner.writestr("publication.db", b"synthetic")
        inner.writestr("images/image.jpg", image)
    with zipfile.ZipFile(path, "w") as outer:
        outer.writestr("contents", contents.getvalue())
    return hashlib.md5(path.read_bytes()).hexdigest()


def _prepared_worker(monkeypatch, tmp_path, first_publisher_suffix=None):
    assets = PublicationAssetStore(tmp_path / "media_assets")
    if first_publisher_suffix:
        first = tmp_path / f"first{first_publisher_suffix}"
        first.write_bytes(b"original")
        assets.materialize(first)
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    worker = JwpubWorker(tmp_path / "jwpub", checksums, asset_store=assets)
    archive = worker._cache.jwpub_path("mwb", "T", "20260500")
    checksum = _archive(archive)
    checksums.save("mwb", "T", "20260500", checksum)
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/source", checksum),
    )
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda root, *_: MwbPublicationContent(
            "week",
            [
                MeetingMedia(
                    multimedia_id=1,
                    mime_type="image/jpeg",
                    file_path=str(root / "images/image.jpg"),
                )
            ],
            [],
            b"cover",
            None,
        ),
    )
    results = []
    worker.mwb_done.connect(lambda _key, value: results.append(value))
    worker.load_week(
        MONDAY, content_request=PublicationContentRequest(publications=frozenset({"mwb"}))
    )
    assert results and results[-1].mwb_status == "ready"
    worker.release_assets((MONDAY.isoformat(), "T", False, 0))
    return worker, assets, results[-1], archive


@pytest.mark.parametrize(
    "operation", [MeetingContentOperation.PREPARE, MeetingContentOperation.UPDATE]
)
@pytest.mark.parametrize("force", [False, True])
def test_all_cached_meeting_content_is_published_before_remote_queries(
    monkeypatch, tmp_path, operation, force
):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    for pub, issue in (("book", "0"), ("w", "20260400")):
        checksum = _archive(worker._cache.jwpub_path(pub, "T", issue))
        worker._checksum_store.save(pub, "T", issue, checksum)
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda *_: MwbPublicationContent(
            "week", [], [MeetingPublicationRef(pub="book", is_cbs=True, meps_doc_id=8)], None, None
        ),
    )
    monkeypatch.setattr(worker_module, "watchtower_issue_contains_week", lambda *_: True)
    monkeypatch.setattr(
        worker_module,
        "read_watchtower_study_content",
        lambda *_: WatchtowerPublicationContent("weekend", [], None),
    )
    monkeypatch.setattr(worker, "_parse_ref_items", lambda *_: [MeetingMedia(multimedia_id=8)])
    events = []
    worker.mwb_done.connect(lambda _key, value: events.append(("mwb", value)))
    worker.wt_done.connect(lambda _key, value: events.append(("wt", value)))
    worker.cbs_done.connect(lambda _key, value: events.append(("refs", value)))

    def resolve(pub, lang, issue):
        assert {kind for kind, _ in events} == {"mwb", "wt", "refs"}
        cached_refs = next(value for kind, value in events if kind == "refs")
        assert cached_refs.cbs_status == "loading"
        assert cached_refs.mwb_publication_refs[0].items
        return JwpubMediaInfo(None, worker._checksum_store.get(pub, lang, issue))

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    worker.load_week(
        MONDAY,
        force=force,
        generation=1,
        known_wt_issue="20260400",
        content_request=PublicationContentRequest(operation=operation),
    )
    assert {kind for kind, _ in events} == {"mwb", "wt", "refs"}
    assert events[-1][1].mwb_status == "ready" or events[-1][0] == "wt"
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


@pytest.mark.parametrize("failure", ["obtain", "parse"])
def test_reference_failures_do_not_suppress_healthy_reference_results(
    monkeypatch, tmp_path, failure
):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    refs = [
        MeetingPublicationRef(pub=pub, meps_doc_id=index)
        for index, pub in enumerate(("bad", "healthy"), 1)
    ]
    for ref in refs:
        checksum = _archive(worker._cache.jwpub_path(ref.pub, "T", "0"))
        worker._checksum_store.save(ref.pub, "T", "0", checksum)
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda *_: MwbPublicationContent("week", [], refs, None, None),
    )
    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", lambda *_: JwpubMediaInfo(None))
    obtain = worker._obtain_publication

    def obtain_publication(pub, *args, **kwargs):
        if pub == "bad" and failure == "obtain":
            raise OSError("Reference request failed")
        return obtain(pub, *args, **kwargs)

    def parse(pub, *_):
        if pub == "bad":
            if failure == "parse":
                raise OSError("Reference parse failed")
            return []
        return [MeetingMedia(multimedia_id=2)]

    monkeypatch.setattr(worker, "_obtain_publication", obtain_publication)
    monkeypatch.setattr(worker, "_parse_ref_items", parse)
    results = []
    worker.cbs_done.connect(lambda _key, value: results.append(value))
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(publications=frozenset({"mwb"})),
    )
    assert results
    final = results[-1]
    assert final.cbs_status == "error"
    assert any(ref.pub == "healthy" and ref.items for ref in final.mwb_publication_refs)
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


@pytest.mark.parametrize("pub_type", ["mwb", "wt"])
@pytest.mark.parametrize("download_fails", [False, True])
def test_shared_parent_cache_cannot_replace_a_different_applied_revision_without_confirmation(
    monkeypatch, tmp_path, pub_type, download_fails
):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    pub, issue = ("mwb", "20260500") if pub_type == "mwb" else ("w", "20260400")
    if pub_type == "wt":
        checksum = _archive(worker._cache.jwpub_path(pub, "T", issue))
        worker._checksum_store.save(pub, "T", issue, checksum)
        monkeypatch.setattr(worker_module, "watchtower_issue_contains_week", lambda *_: True)
        monkeypatch.setattr(
            worker_module,
            "read_watchtower_study_content",
            lambda *_: WatchtowerPublicationContent("weekend", [], None),
        )
    results = []
    worker.mwb_done.connect(lambda _key, value: results.append(value))
    worker.wt_done.connect(lambda _key, value: results.append(value))
    requested = []

    def resolve(*source):
        assert not results, "A different cached revision must not be served before confirmation"
        requested.append(source)
        return JwpubMediaInfo("https://invalid.test/unavailable" if download_fails else None)

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    monkeypatch.setattr(worker, "_download", lambda *_args, **_kwargs: False)
    worker.load_week(
        MONDAY,
        force=True,
        generation=1,
        known_wt_issue=issue if pub_type == "wt" else "",
        content_request=PublicationContentRequest(
            publications=frozenset({pub_type}),
            operation=MeetingContentOperation.UPDATE,
            dependencies={
                pub_type: [{"pub": pub, "language": "T", "issue": issue, "checksum": "applied"}]
            },
        ),
    )
    assert not results
    assert requested == [(pub, "T", issue)]
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


@pytest.mark.parametrize("applied_issue", ["0", "20070601"])
def test_shared_reference_cache_does_not_replace_an_unverified_applied_source(
    monkeypatch, tmp_path, applied_issue
):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    checksum = _archive(worker._cache.jwpub_path("book", "T", "0"))
    worker._checksum_store.save("book", "T", "0", checksum)
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda *_: MwbPublicationContent(
            "week",
            [],
            [MeetingPublicationRef(pub="book", issue="20070601", meps_doc_id=8)],
            None,
            None,
        ),
    )
    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", lambda *_: JwpubMediaInfo(None))
    parsed = []
    monkeypatch.setattr(
        worker,
        "_parse_ref_items",
        lambda *_: parsed.append(True) or [MeetingMedia(multimedia_id=8)],
    )
    results = []
    worker.cbs_done.connect(lambda _key, value: results.append(value))
    worker.load_week(
        MONDAY,
        force=True,
        generation=1,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.UPDATE,
            dependencies={
                "mwb": [
                    {"pub": "book", "language": "T", "issue": applied_issue, "checksum": "applied"}
                ]
            },
        ),
    )
    assert not parsed
    assert results[-1].cbs_status == "error"
    assert not results[-1].mwb_publication_refs
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


@pytest.mark.parametrize("changed_pub", ["mwb", "w", "book"])
def test_revalidation_publishes_changed_images_without_mutating_cached_results(
    monkeypatch, tmp_path, changed_pub
):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    for pub, issue in (("w", "20260400"), ("book", "0")):
        checksum = _archive(worker._cache.jwpub_path(pub, "T", issue))
        worker._checksum_store.save(pub, "T", issue, checksum)
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda root, *_: MwbPublicationContent(
            "week",
            [
                MeetingMedia(
                    multimedia_id=1,
                    mime_type="image/jpeg",
                    file_path=str(root / "images/image.jpg"),
                )
            ],
            [MeetingPublicationRef(pub="book", meps_doc_id=8)],
            None,
            None,
        ),
    )
    monkeypatch.setattr(worker_module, "watchtower_issue_contains_week", lambda *_: True)
    monkeypatch.setattr(
        worker_module,
        "read_watchtower_study_content",
        lambda root, *_: WatchtowerPublicationContent(
            "weekend",
            [
                MeetingMedia(
                    multimedia_id=2,
                    mime_type="image/jpeg",
                    file_path=str(root / "images/image.jpg"),
                )
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "parse_publication_ref_items",
        lambda root, *_args, **_kwargs: [
            MeetingMedia(
                multimedia_id=8, mime_type="image/jpeg", file_path=str(root / "images/image.jpg")
            )
        ],
    )
    replacement = tmp_path / "replacement.jwpub"
    new_checksum = _archive(replacement, b"changed")
    downloaded = []

    def resolve(pub, lang, issue):
        checksum = (
            new_checksum if pub == changed_pub else worker._checksum_store.get(pub, lang, issue)
        )
        return JwpubMediaInfo("https://invalid.test/source", checksum)

    def download(pub, lang, issue, *_args, **_kwargs):
        downloaded.append(pub)
        worker._cache.jwpub_path(pub, lang, issue).write_bytes(replacement.read_bytes())
        worker._cache.mark_extract_stale(pub, lang, issue)
        return True

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    monkeypatch.setattr(worker, "_download", download)
    results = {"mwb": [], "w": [], "book": []}
    worker.mwb_done.connect(lambda _key, value: results["mwb"].append(value))
    worker.wt_done.connect(lambda _key, value: results["w"].append(value))
    worker.cbs_done.connect(lambda _key, value: results["book"].append(value))
    worker.load_week(
        MONDAY,
        force=True,
        generation=1,
        known_wt_issue="20260400",
        content_request=PublicationContentRequest(operation=MeetingContentOperation.UPDATE),
    )
    try:
        assert downloaded == [changed_pub]
        snapshots = results[changed_pub]

        def media(snapshot):
            if changed_pub == "mwb":
                return snapshot.mwb_all_media
            if changed_pub == "w":
                return snapshot.wt_all_media
            return snapshot.mwb_publication_refs[0].items

        assert Path(media(snapshots[0])[0].file_path).read_bytes() == b"original"
        assert Path(media(snapshots[-1])[0].file_path).read_bytes() == b"changed"
        assert media(snapshots[0])[0].asset_hash != media(snapshots[-1])[0].asset_hash
        assert not list(worker._cache._root.glob("*/.x_*"))
    finally:
        worker.release_assets((MONDAY.isoformat(), "T", False, 1))


def test_cancellation_after_cached_base_prevents_all_remote_queries(monkeypatch, tmp_path):
    worker, _assets, _result, _path = _prepared_worker(monkeypatch, tmp_path)
    worker.mwb_done.connect(lambda *_: worker.cancel_request((MONDAY.isoformat(), "T", False, 1)))
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: pytest.fail("Cancelled work must not query metadata"),
    )
    finished = []
    worker.load_finished.connect(lambda *context: finished.append(context))
    worker.load_week(MONDAY, force=True, generation=1, content_request=PublicationContentRequest())
    assert finished == [(MONDAY.isoformat(), "T", False, 1)]
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


def test_preparation_emits_durable_images_and_removes_all_extracted_files(monkeypatch, tmp_path):
    worker, assets, result, archive = _prepared_worker(monkeypatch, tmp_path)
    image = result.mwb_all_media[0]
    assert assets.owns(image.file_path)
    assert Path(image.file_path).read_bytes() == b"original"
    assert image.publication_source["member"] == "images/image.jpg"
    assert image.asset_hash == hashlib.sha256(b"original").hexdigest()
    assert not worker._cache.extract_dir("mwb", "T", "20260500").exists()
    archive.unlink()
    assert Path(image.file_path).read_bytes() == b"original"


@pytest.mark.parametrize("cancel_on_failure", [False, True])
def test_missing_midweek_image_does_not_fail_weekend_preparation(
    monkeypatch, tmp_path, cancel_on_failure
):
    worker, _assets, _result, _archive_path = _prepared_worker(monkeypatch, tmp_path)
    attempted = []
    errors = []
    finished = []
    worker.error.connect(lambda _key, pub, *_: errors.append(pub))
    worker.load_finished.connect(lambda *context: finished.append(context))
    monkeypatch.setattr(worker, "_obtain_publication", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker, "_resolve_archive", lambda *_: JwpubMediaInfo(None))

    def missing_image(root, *_):
        if cancel_on_failure:
            worker.cancel_request((MONDAY.isoformat(), "T", False, 1))
        return MwbPublicationContent(
            "week",
            [MeetingMedia(mime_type="image/jpeg", file_path=str(root / "missing.jpg"))],
            [],
            None,
            None,
        )

    monkeypatch.setattr(worker_module, "read_mwb_week_content", missing_image)
    monkeypatch.setattr(worker, "_try_wt_cached", lambda *_: attempted.append("wt") or True)

    worker.load_week(MONDAY, generation=1, content_request=PublicationContentRequest())

    assert errors == ([] if cancel_on_failure else ["mwb"])
    assert attempted == ([] if cancel_on_failure else ["wt"])
    assert finished == [(MONDAY.isoformat(), "T", False, 1)]
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


@pytest.mark.parametrize("first_publisher_suffix", [None, ".jpeg"])
def test_repair_restores_exact_image_from_local_archive_without_remote_queries(
    monkeypatch, tmp_path, first_publisher_suffix
):
    worker, assets, result, _archive_path = _prepared_worker(
        monkeypatch, tmp_path, first_publisher_suffix
    )
    image = result.mwb_all_media[0]
    assets.remove(image.file_path)
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: pytest.fail("Local repair must precede network"),
    )
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.REPAIR,
            repair_refs=(asdict(image),),
        ),
    )
    assert Path(image.file_path).read_bytes() == b"original"
    assert not worker._cache.extract_dir("mwb", "T", "20260500").exists()
    worker.release_assets((MONDAY.isoformat(), "T", False, 1))


def test_repair_refuses_changed_bytes_from_remote_revision(monkeypatch, tmp_path):
    worker, assets, result, archive = _prepared_worker(monkeypatch, tmp_path)
    image = result.mwb_all_media[0]
    assets.remove(image.file_path)
    archive.unlink()
    worker._remote_observations.clear()
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/new", "new"),
    )
    monkeypatch.setattr(
        worker, "_download", lambda *_args, **_kwargs: bool(_archive(archive, b"different"))
    )
    errors = []
    worker.error.connect(lambda *_args: errors.append(_args[2]))
    before = asdict(image)
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.REPAIR,
            repair_refs=(asdict(image),),
        ),
    )
    assert not Path(image.file_path).exists()
    assert asdict(image) == before
    assert errors
    assert assets.list_items() == []


@pytest.mark.parametrize("remote_checksum_known", [False, True])
@pytest.mark.parametrize("changed_cache", [False, True])
def test_unrecoverable_image_automatically_rebuilds_saved_meeting_with_truthful_revision(
    monkeypatch, tmp_path, remote_checksum_known, changed_cache
):
    worker, assets, original, archive = _prepared_worker(monkeypatch, tmp_path)
    store = MeetingTreeStore(tmp_path / "trees.json")
    builder = MeetingTreeBuilder()
    nodes = builder.build_midweek(original)
    tree_key = "mwb:2026-05-25:T:20260500"
    store.save(
        tree_key,
        nodes,
        builder.canonical_hash(nodes),
        canonical_nodes=nodes,
        source_checksum=original.mwb_source_checksum,
        publication_dependencies=original.mwb_dependencies,
    )
    assets.remove(original.mwb_all_media[0].file_path)
    archive.unlink()
    worker._remote_observations.clear()
    replacement = tmp_path / "replacement.jwpub"
    checksum = _archive(replacement, b"changed")
    if changed_cache:
        archive.write_bytes(replacement.read_bytes())
        worker._checksum_store.save("mwb", "T", "20260500", checksum)
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo(
            "https://invalid.test/new", checksum if remote_checksum_known else ""
        ),
    )
    downloads = []

    def download(_url, destination, **_kwargs):
        downloads.append(destination)
        destination.write_bytes(replacement.read_bytes())

    monkeypatch.setattr(worker_module, "download_jwpub_archive", download)
    loads = []
    publication = SimpleNamespace(
        mwb_ready=worker.mwb_done,
        wt_ready=worker.wt_done,
        cbs_ready=worker.cbs_done,
        context_progress=worker.progress,
        context_error=worker.error,
        context_load_finished=worker.load_finished,
        content_checked=worker.content_checked,
        load_week=lambda monday, force=False, **context: loads.append((monday, force, context)),
        shutdown=lambda **_: None,
    )
    # A manual initial-download preference never authorizes audio/video prefetch here.
    cache = SimpleNamespace(prefetch_many=lambda *_: pytest.fail("Unexpected AV prefetch"))
    service = MeetingPreparationService(publication, store, cache)
    service._today = lambda: MONDAY
    key = MeetingPreparationKey(MONDAY, "T")
    try:
        service.repair_meeting(key, "mwb")
        index = 0
        while index < len(loads) and index < 3:
            monday, force, context = loads[index]
            worker.load_week(
                monday,
                force=force,
                language=context["language_code"],
                is_sign_language=context["is_sign_language"],
                generation=context["generation"],
                known_wt_issue=context["known_wt_issue"],
                content_request=context["content_request"],
            )
            worker.release_assets((MONDAY.isoformat(), "T", False, context["generation"]))
            index += 1

        assert [context["content_request"].operation for _, _, context in loads] == [
            MeetingContentOperation.REPAIR,
            MeetingContentOperation.UPDATE,
        ]
        saved = store.snapshot(tree_key)
        images = [
            node["media_ref"]
            for node in iter_nodes(saved.nodes)
            if node.get("type") == "media" and node.get("media_type") == "image"
        ]
        assert images and all(Path(ref["file_path"]).read_bytes() == b"changed" for ref in images)
        assert saved.source_checksum == checksum
        assert saved.publication_dependencies[0]["checksum"] == checksum
        assert all(ref["publication_source"]["checksum"] == checksum for ref in images)
        assert not service.state(key, "mwb").repair_required
        assert not service.state(key, "mwb").repair_requires_update
        assert downloads
        # Fresh-download authorization belongs to one request, never the shared cache.
        results = []
        worker.mwb_done.connect(lambda *_args: results.append(_args[1]))
        monkeypatch.setattr(worker_module, "resolve_jwpub_archive", lambda *_: JwpubMediaInfo(None))
        worker._remote_observations.clear()
        worker.load_week(
            MONDAY,
            generation=99,
            content_request=PublicationContentRequest(
                publications=frozenset({"mwb"}),
                operation=MeetingContentOperation.UPDATE,
                dependencies={"mwb": original.mwb_dependencies},
            ),
        )
        worker.release_assets((MONDAY.isoformat(), "T", False, 99))
        assert results == []
    finally:
        service.shutdown(wait_ms=1000)


def test_unknown_download_identity_cannot_reuse_previous_weekly_checksum(monkeypatch, tmp_path):
    worker, assets, original, archive = _prepared_worker(monkeypatch, tmp_path)
    image = original.mwb_all_media[0]
    assets.remove(image.file_path)
    archive.unlink()
    worker._remote_observations.clear()
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/new"),
    )
    monkeypatch.setattr(
        worker_module,
        "download_jwpub_archive",
        lambda _url, destination, **_: _archive(destination, b"changed"),
    )
    monkeypatch.setattr(worker_module, "local_jwpub_checksum", lambda *_: "")
    errors = []
    worker.error.connect(lambda *_args: errors.append(_args[2]))
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.REPAIR,
            repair_refs=(asdict(image),),
        ),
    )
    try:
        assert errors
        assert worker._checksum_store.get("mwb", "T", "20260500") == ""
        assert not Path(image.file_path).exists()
    finally:
        worker.release_assets((MONDAY.isoformat(), "T", False, 1))


def test_checks_are_lightweight_and_deduplicated_for_parent_and_book(monkeypatch, tmp_path):
    worker = JwpubWorker(tmp_path / "jwpub", JwpubChecksumStore(tmp_path / "checksums.json"))
    requests = []
    clock = [0.0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])

    def resolve(pub, language, issue):
        requests.append((pub, language, issue))
        return JwpubMediaInfo("https://invalid.test/source", "new")

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    monkeypatch.setattr(
        worker, "_download", lambda *_args, **_kwargs: pytest.fail("A check cannot download")
    )
    command = PublicationContentRequest(
        publications=frozenset({"mwb"}),
        operation=MeetingContentOperation.CHECK,
        dependencies={
            "mwb": [
                {"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "old"},
                {"pub": "book", "language": "T", "issue": "0", "checksum": "old"},
            ]
        },
    )
    worker.load_week(MONDAY, content_request=command)
    worker.load_week(MONDAY, content_request=command)
    assert len(requests) == 2
    clock[0] = 6 * 60 * 60
    worker.load_week(MONDAY, content_request=command)
    assert len(requests) == 4


def test_policy_factory_reads_current_global_defaults(monkeypatch):
    from solin.core.foundation import constants

    monkeypatch.setattr(constants, "KEEP_MEETING_IMAGES_FOR_MANUAL_CLEANUP", True)
    monkeypatch.setattr(constants, "AUTO_RECOVER_PREPARED_MEETING_IMAGES", False)
    monkeypatch.setattr(constants, "AUTO_UPDATE_PREPARED_MEETINGS", False)
    assert MeetingContentPolicy.from_defaults() == MeetingContentPolicy(True, False, False)


@pytest.mark.parametrize(
    "scenario, completes",
    [
        ("optional_unpublished", True),
        ("optional_transient", False),
        ("optional_previously_applied", False),
        ("required_unpublished", True),
        ("optional_corrupt", False),
        ("empty_cbs", True),
        ("parent_removed_reference", True),
        ("cancelled_reference", False),
    ],
)
def test_midweek_reference_availability_keeps_base_usable_and_confirms_only_complete_content(
    monkeypatch, tmp_path, scenario, completes
):
    worker, _assets, _prepared, _parent_archive = _prepared_worker(monkeypatch, tmp_path)
    legacy = MeetingPublicationRef(
        pub="legacy",
        issue="20070601",
        section="lac",
        is_cbs=scenario == "required_unpublished",
        meps_doc_id=7,
    )
    book = MeetingPublicationRef(pub="book", is_cbs=True, meps_doc_id=8)
    refs = [book] if scenario in {"parent_removed_reference", "empty_cbs"} else [legacy, book]
    for pub, issue in (("book", "0"), ("legacy", "20070601")):
        if pub == "legacy" and scenario != "optional_corrupt":
            continue
        checksum = _archive(worker._cache.jwpub_path(pub, "T", issue))
        worker._checksum_store.save(pub, "T", issue, checksum)
    requested = []
    base = []
    worker.mwb_done.connect(lambda _key, value: base.append(value))

    def resolve(pub, _lang, issue):
        if pub != "mwb":
            assert base, "The base meeting must be published before resolving references"
        requested.append((pub, issue))
        if pub == "legacy" and scenario == "cancelled_reference":
            worker.cancel_request((MONDAY.isoformat(), "T", False, 1))
        return JwpubMediaInfo(
            None, not_found=(pub == "legacy" and scenario != "optional_transient")
        )

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    monkeypatch.setattr(
        worker_module,
        "read_mwb_week_content",
        lambda *_: MwbPublicationContent("week", [], refs, None, None),
    )

    def parse(pub, *_):
        if pub == "legacy" and scenario == "optional_corrupt":
            raise OSError("corrupt referenced publication")
        return []

    monkeypatch.setattr(worker, "_parse_ref_items", parse)
    applied = [{"pub": "legacy", "language": "T", "issue": "20070601", "checksum": "old"}]
    results = []
    errors = []
    worker.cbs_done.connect(lambda _key, value: results.append(value))
    worker.error.connect(lambda *_args: errors.append(_args[2]))
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.UPDATE,
            dependencies={"mwb": applied}
            if scenario in {"optional_previously_applied", "parent_removed_reference"}
            else {},
        ),
    )
    try:
        assert len(base) == 1
        assert base[0].mwb_status == "ready"
        assert base[0].cbs_status == "loading"
        assert {dependency["pub"] for dependency in base[0].mwb_dependencies} == {"mwb"}
        assert results
        assert not errors
        if scenario == "cancelled_reference":
            assert ("legacy", "0") not in requested
            assert all(result.cbs_status == "loading" for result in results)
        elif scenario != "optional_corrupt":
            assert ("book", "0") in requested
            assert results[-1].cbs_status == ("ready" if completes else "error")
            assert {dependency["pub"] for dependency in results[-1].mwb_dependencies} == {
                "mwb",
                "book",
            }
        else:
            assert any(ref.pub == "book" for ref in results[-1].mwb_publication_refs)
            assert results[-1].cbs_status == "error"
    finally:
        worker.release_assets((MONDAY.isoformat(), "T", False, 1))
