from __future__ import annotations

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier, Event

import pytest

from solin.core.media import publication_assets
from solin.core.media.cache_listing import CachedMediaItem
from solin.core.media.publication_assets import PublicationAssetStore
from solin.core.storage import binary_files


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "publication.jpg"
    path.write_bytes(b"original publication image")
    return path


def _metadata(store: PublicationAssetStore) -> Path:
    return next(store.root.glob("*.json"))


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as error:
        if os.name == "nt" and target.is_dir():
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return
        pytest.skip(f"Symlinks unavailable: {error}")


def test_materialize_deduplicates_original_bytes_across_names_and_extensions(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    duplicate = tmp_path / "renamed.png"
    duplicate.write_bytes(source.read_bytes())

    first = store.materialize(source, title="Publication illustration")
    second = store.materialize(duplicate, title="Another title")

    assert first == second
    assert Path(first).is_absolute()
    assert store.asset_id(first) == hashlib.sha256(source.read_bytes()).hexdigest()
    assert store.root == (tmp_path / "assets").resolve()
    assert store.owns(first)
    assert len(store.list_items()) == 1
    assert not list(store.root.rglob("*.done"))


def test_materialize_keeps_original_bytes_independent_of_source_lifetime(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    original = source.read_bytes()
    first = store.materialize(source)
    source.write_bytes(b"new publication revision")
    second = store.materialize(source)
    source.unlink()

    assert first != second
    assert Path(first).read_bytes() == original
    assert Path(second).read_bytes() == b"new publication revision"


def test_listing_survives_a_new_store_without_http_markers(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source, title="  Illustration  ")

    items = PublicationAssetStore(store.root).list_items()

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, CachedMediaItem)
    assert item.path == path
    assert item.filename == source.name
    assert item.display_title == "Illustration"
    assert item.size == len(source.read_bytes())
    assert item.media_type == "image"
    assert item.original_url == ""


def test_default_title_uses_original_filename(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    store.materialize(source)

    assert store.list_items()[0].display_title == source.stem


def test_concurrent_publishers_share_one_immutable_object(tmp_path: Path, source: Path) -> None:
    root = tmp_path / "assets"
    barrier = Barrier(8)

    def publish(index: int) -> str:
        store = PublicationAssetStore(root / ".." / root.name)
        barrier.wait(timeout=10)
        return store.materialize(source, title=f"Illustration {index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(publish, range(8)))

    assert len(set(paths)) == 1
    assert Path(paths[0]).read_bytes() == source.read_bytes()
    assert len(PublicationAssetStore(root).list_items()) == 1
    assert all(not path.name.startswith(".") for path in root.iterdir())


def test_existing_conflicting_bytes_are_never_overwritten(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    path.write_bytes(b"corrupt existing object")

    with pytest.raises(OSError):
        store.materialize(source)

    assert path.read_bytes() == b"corrupt existing object"


def test_warm_materialize_does_not_create_temporary_files_or_fsync(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)

    def unexpected_write(*_args, **_kwargs):
        pytest.fail("A warm immutable object must not create temporary files or fsync")

    with monkeypatch.context() as patch:
        patch.setattr(binary_files.tempfile, "mkstemp", unexpected_write)
        patch.setattr(binary_files.os, "fsync", unexpected_write)
        assert store.materialize(source) == path

    assert Path(path).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("identical", [False, True])
def test_materialize_keeps_immutable_writer_conflict_handling_after_a_cache_miss(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    identical: bool,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    original = source.read_bytes()
    target = store.root / f"{hashlib.sha256(original).hexdigest()}{source.suffix}"
    competing = original if identical else b"competing bytes"
    publish = publication_assets.publish_bytes_immutable

    def publish_after_competitor(path, payload, **kwargs):
        if Path(path) == target:
            target.write_bytes(competing)
        return publish(path, payload, **kwargs)

    monkeypatch.setattr(publication_assets, "publish_bytes_immutable", publish_after_competitor)

    if identical:
        assert store.materialize(source) == str(target)
    else:
        with pytest.raises(FileExistsError):
            store.materialize(source)
    assert target.read_bytes() == competing


def test_only_owned_regular_images_can_be_removed(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    unrelated = store.root / "unrelated.jpg"
    unrelated.write_bytes(b"not an object")

    for foreign in (source, store.root, unrelated, _metadata(store)):
        assert not store.owns(foreign)
        assert not store.remove(foreign)
        with pytest.raises(ValueError):
            store.asset_id(foreign)

    assert store.remove(path)
    assert not store.remove(path)
    assert source.exists()
    assert unrelated.exists()
    assert store.list_items() == []


def test_symlinked_asset_never_deletes_its_target(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    path.unlink()
    _symlink(path, source)

    assert not store.owns(path)
    assert not store.remove(path)
    assert store.list_items() == []
    assert store.collect(set) == []
    assert source.exists()


def test_symlink_alias_to_an_owned_asset_is_not_owned(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    alias = tmp_path / "alias.jpg"
    _symlink(alias, path)

    assert not store.owns(alias)
    assert not store.remove(alias)
    assert path.exists()


def test_symlinked_metadata_is_never_read_or_used_for_removal(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    metadata = _metadata(store)
    outside = tmp_path / "outside.json"
    original_metadata = metadata.read_bytes()
    outside.write_bytes(original_metadata)
    metadata.unlink()
    _symlink(metadata, outside)

    assert store.list_items() == []
    assert store.collect(set) == []
    with pytest.raises(OSError):
        store.remove(path)
    with pytest.raises(OSError):
        store.materialize(source)
    assert Path(path).exists()
    assert outside.read_bytes() == original_metadata


def test_replaced_root_cannot_redirect_removal(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    moved = tmp_path / "moved"
    store.root.rename(moved)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / path.name
    victim.write_bytes(b"external image")
    _symlink(store.root, outside)

    assert not store.owns(path)
    assert not store.remove(path)
    with pytest.raises(OSError):
        store.materialize(source)
    assert victim.read_bytes() == b"external image"


@pytest.mark.parametrize("replace_ancestor", [False, True])
def test_root_swap_after_metadata_validation_cannot_delete_foreign_files(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_ancestor: bool,
) -> None:
    store = PublicationAssetStore(tmp_path / "container" / "assets")
    path = Path(store.materialize(source))
    outside = tmp_path / "outside" / "assets"
    outside.mkdir(parents=True)
    victim = outside / path.name
    victim.write_bytes(b"external image")
    metadata = _metadata(store)
    victim_metadata = outside / metadata.name
    victim_metadata.write_bytes(metadata.read_bytes())
    read_metadata = store._read_metadata

    def swap_after_validation(identifier):
        result = read_metadata(identifier)
        replaced = store.root.parent if replace_ancestor else store.root
        replacement = outside.parent if replace_ancestor else outside
        replaced.rename(tmp_path / "moved")
        _symlink(replaced, replacement)
        return result

    monkeypatch.setattr(store, "_read_metadata", swap_after_validation)
    try:
        store.remove(path)
    except OSError:
        # Windows may reject the rename while the directory handle is held.
        pass

    assert victim.read_bytes() == b"external image"
    assert victim_metadata.exists()


def test_nested_leases_protect_across_instances_and_unwind_after_errors(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)
    path = store.materialize(source)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with store.lease([path, path]):
            with other.lease([path]):
                with pytest.raises(OSError):
                    other.remove(path)
            assert other.collect(set) == []
            with pytest.raises(OSError):
                other.remove(path)
            raise RuntimeError("consumer failed")

    assert other.remove(path)


@pytest.mark.parametrize("replace_metadata", [False, True])
def test_leaf_link_inserted_after_validation_is_not_removed(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_metadata: bool,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    leaf = _metadata(store) if replace_metadata else path
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "image.jpg"
    victim.write_bytes(b"external image")
    read_metadata = store._read_metadata

    def replace_after_validation(identifier):
        result = read_metadata(identifier)
        leaf.unlink()
        _symlink(leaf, outside)
        return result

    monkeypatch.setattr(store, "_read_metadata", replace_after_validation)

    with pytest.raises(OSError):
        store.remove(path)

    assert leaf.is_symlink() or leaf.is_junction()
    assert victim.read_bytes() == b"external image"


def test_owner_pins_replace_atomically_and_combine_with_other_owners_and_leases(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)
    first = store.materialize(source)
    source.write_bytes(b"another image")
    second = store.materialize(source)
    store.retain("worker", [first])
    other.retain("another worker", [first])
    store.retain("worker", [second])

    with pytest.raises(OSError):
        store.remove(first)
    other.release("another worker")
    assert other.remove(first)
    with store.lease([second]):
        other.release("worker")
        assert other.collect(set) == []
    assert other.remove(second)
    other.release("unknown")


def test_failed_pin_update_preserves_previous_owner_pins(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    store.retain("worker", [path])

    with pytest.raises((TypeError, ValueError)):
        store.retain("worker", [None])  # type: ignore[list-item]
    with pytest.raises(OSError):
        store.remove(path)

    store.release("worker")
    assert store.remove(path)


def test_filesystem_error_cannot_silently_discard_pins(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    store.retain("worker", [str(path)])
    path_stat = Path.stat

    def unreadable_asset(target, *args, **kwargs):
        if target == path:
            raise PermissionError("asset temporarily unreadable")
        return path_stat(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", unreadable_asset)
        with pytest.raises(OSError):
            store.retain("worker", [str(path)])
        with pytest.raises(OSError):
            with store.lease([str(path)]):
                pytest.fail("An unsafe lease must not start")

    assert store.collect(set) == []
    store.release("worker")
    assert store.remove(path)


def test_source_lease_closes_the_publication_to_owner_handoff_gap(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)

    with store.lease([str(source)]):
        path = store.materialize(source)
        assert other.collect(set) == []
        with pytest.raises(OSError):
            other.remove(path)
        store.retain("publication", [path])

    assert other.collect(set) == []
    other.release("publication")
    assert other.remove(path)


def test_planned_asset_lease_protects_publication_across_extension_deduplication(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = str(store.root / f"{digest}.png")

    with store.lease([candidate]):
        path = store.materialize(source)
        assert Path(path).suffix == ".jpg"
        assert store.collect(set) == []
        store.retain("publication", [path])

    assert store.collect(set) == []
    store.release("publication")
    assert store.remove(path)


def test_concurrent_delete_cannot_remove_an_active_lease(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)
    path = store.materialize(source)
    entered = Event()
    finish = Event()

    def use_asset() -> None:
        with store.lease([path]):
            entered.set()
            assert finish.wait(timeout=10)
            assert Path(path).read_bytes() == source.read_bytes()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(use_asset)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(OSError):
                other.remove(path)
            assert other.collect(set) == []
        finally:
            finish.set()
        future.result(timeout=10)

    assert other.remove(path)


def test_pins_do_not_block_unrelated_roots(tmp_path: Path, source: Path) -> None:
    first = PublicationAssetStore(tmp_path / "one")
    second = PublicationAssetStore(tmp_path / "two")
    first_path = first.materialize(source)
    second_path = second.materialize(source)

    with first.lease([first_path]):
        assert second.remove(second_path)


def test_collect_keeps_references_and_pins_and_removes_only_orphans(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    paths = []
    for index in range(4):
        source.write_bytes(f"image {index}".encode())
        paths.append(store.materialize(source))
    store.retain("publication", [paths[2]])

    removed = store.collect(lambda: {paths[0], store.asset_id(paths[1]), str(source)})

    assert removed == [paths[3]]
    assert all(Path(path).exists() for path in paths[:3])
    store.release("publication")
    assert set(store.collect(set)) == set(paths[:3])


@pytest.mark.parametrize("protection", ["path", "digest", "lease", "owner"])
def test_collect_does_not_inspect_protected_objects(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    protection: str,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    metadata = _metadata(store)
    digest = store.asset_id(path)
    references = (
        {str(path)} if protection == "path" else {digest} if protection == "digest" else set()
    )
    context = store.lease([str(path)]) if protection == "lease" else nullcontext()
    path_stat = Path.stat

    def inspect_only_candidates(target, *args, **kwargs):
        if target in (path, metadata):
            pytest.fail("Collection must not inspect a referenced or pinned object")
        return path_stat(target, *args, **kwargs)

    if protection == "owner":
        store.retain("worker", [str(path)])
    try:
        with context, monkeypatch.context() as patch:
            patch.setattr(Path, "stat", inspect_only_candidates)
            assert store.collect(lambda: references) == []
    finally:
        store.release("worker")

    assert path.exists()
    assert metadata.exists()


@pytest.mark.parametrize("references", [None, [], "", {None}, {""}, {"invalid digest"}])
def test_collect_fails_closed_on_malformed_reference_snapshots(
    tmp_path: Path,
    source: Path,
    references,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)

    with pytest.raises((TypeError, ValueError)):
        store.collect(lambda: references)

    assert Path(path).exists()


def test_collect_propagates_reference_errors_before_removing_anything(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)

    def corrupt_references() -> set[str]:
        raise json.JSONDecodeError("corrupt persisted references", "", 0)

    with pytest.raises(json.JSONDecodeError):
        store.collect(corrupt_references)

    assert Path(path).exists()


def test_collect_serializes_reference_snapshot_with_other_instance_publication(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)
    entered = Event()
    publishing = Event()
    published = Event()

    def publish() -> str:
        assert entered.wait(timeout=10)
        publishing.set()
        path = other.materialize(source)
        published.set()
        return path

    def references() -> set[str]:
        entered.set()
        assert publishing.wait(timeout=10)
        assert not published.wait(timeout=0.1)
        return set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(publish)
        assert store.collect(references) == []
        path = future.result(timeout=10)

    assert Path(path).exists()


@pytest.mark.parametrize(
    "metadata", [b"{broken", b"[]", b'{"filename":"../outside.jpg","title":""}']
)
def test_corrupt_metadata_is_never_replaced_or_used_to_delete_an_object(
    tmp_path: Path,
    source: Path,
    metadata: bytes,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    _metadata(store).write_bytes(metadata)

    assert store.list_items() == []
    assert store.collect(set) == []
    with pytest.raises(OSError):
        store.remove(path)
    with pytest.raises(OSError):
        store.materialize(source)
    assert Path(path).read_bytes() == source.read_bytes()
    assert _metadata(store).read_bytes() == metadata


def test_metadata_write_failure_does_not_publish_an_unlisted_asset(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    publish = publication_assets.publish_bytes_immutable

    def fail_metadata(path, payload, **kwargs):
        if Path(path).suffix == ".json":
            raise OSError("metadata unavailable")
        return publish(path, payload, **kwargs)

    monkeypatch.setattr(publication_assets, "publish_bytes_immutable", fail_metadata)

    with pytest.raises(OSError, match="metadata unavailable"):
        store.materialize(source)

    assert list(store.root.iterdir()) == []


def test_failed_content_publication_is_invisible_and_retryable(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    publish = publication_assets.publish_bytes_immutable

    def fail_content(path, payload, **kwargs):
        if Path(path).suffix != ".json":
            raise OSError("image unavailable")
        return publish(path, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication_assets, "publish_bytes_immutable", fail_content)
        with pytest.raises(OSError, match="image unavailable"):
            store.materialize(source, title="Illustration")

    assert store.list_items() == []
    restarted = PublicationAssetStore(store.root)
    path = restarted.materialize(source)
    assert Path(path).read_bytes() == source.read_bytes()
    assert restarted.list_items()[0].display_title == "Illustration"


def test_collect_cleans_metadata_left_by_interrupted_materialization(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    publish = publication_assets.publish_bytes_immutable

    def fail_content(path, payload, **kwargs):
        if Path(path).suffix != ".json":
            raise OSError("image unavailable")
        return publish(path, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication_assets, "publish_bytes_immutable", fail_content)
        with pytest.raises(OSError, match="image unavailable"):
            store.materialize(source)

    metadata = _metadata(store)
    assert store.list_items() == []
    assert store.collect(set) == []
    assert not metadata.exists()
    assert list(store.root.iterdir()) == []


def test_collect_cleans_metadata_after_partial_removal(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    metadata = _metadata(store)
    unlink = os.unlink

    def fail_metadata_unlink(target, *args, **kwargs):
        if Path(target).name == metadata.name:
            raise PermissionError("metadata is busy")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_metadata_unlink)
        with pytest.raises(OSError, match="metadata is busy"):
            store.collect(set)

    assert not path.exists()
    assert metadata.exists()
    assert store.collect(set) == []
    assert list(store.root.iterdir()) == []


def test_manual_deletion_preserves_path_for_repair_from_another_extension(tmp_path):
    store = PublicationAssetStore(tmp_path / "assets")
    original = tmp_path / "first.jpeg"
    duplicate = tmp_path / "other.jpg"
    original.write_bytes(b"identical publication image")
    duplicate.write_bytes(original.read_bytes())
    saved_path = store.materialize(original)
    assert store.materialize(duplicate) == saved_path

    assert store.remove(saved_path)
    restarted = PublicationAssetStore(store.root)
    assert restarted.collect(lambda: {saved_path}, retain_images=True) == []
    assert restarted.list_items() == []
    assert restarted.materialize(duplicate) == saved_path
    assert Path(saved_path).read_bytes() == original.read_bytes()

    assert restarted.remove(saved_path)
    assert restarted.collect(set, retain_images=True) == []
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("protection", ["path", "digest", "lease", "owner"])
def test_orphan_metadata_respects_references_and_pins(
    tmp_path: Path,
    source: Path,
    protection: str,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    other = PublicationAssetStore(store.root)
    path = store.materialize(source)
    digest = store.asset_id(path)
    metadata = _metadata(store)
    Path(path).unlink()
    references = {path} if protection == "path" else {digest} if protection == "digest" else set()
    context = store.lease([path]) if protection == "lease" else nullcontext()

    if protection == "owner":
        store.retain("worker", [path])
    try:
        with context:
            assert other.collect(lambda: references) == []
            assert metadata.exists()
    finally:
        store.release("worker")

    assert other.collect(set) == []
    assert not metadata.exists()


@pytest.mark.parametrize("corruption", ["invalid", "unreadable"])
def test_orphan_cleanup_preserves_unknown_files_and_untrusted_metadata(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    Path(store.materialize(source)).unlink()
    metadata = _metadata(store)
    unknown = store.root / "user-document.json"
    unknown.write_bytes(b"user data")
    read_bytes = Path.read_bytes
    if corruption == "invalid":
        metadata.write_bytes(b"{broken")
    else:

        def unreadable_metadata(target):
            if target == metadata:
                raise PermissionError("metadata unreadable")
            return read_bytes(target)

        monkeypatch.setattr(Path, "read_bytes", unreadable_metadata)

    assert store.collect(set) == []
    assert metadata.exists()
    assert unknown.read_bytes() == b"user data"


def test_orphan_cleanup_does_not_discard_metadata_with_another_image_entry(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    metadata = _metadata(store)
    metadata.write_text(json.dumps({"filename": "other.png", "title": ""}), encoding="utf-8")

    assert store.list_items() == []
    assert store.collect(set) == []
    assert metadata.exists()
    assert path.read_bytes() == source.read_bytes()


def test_corrupt_reference_provider_protects_orphan_metadata(tmp_path: Path, source: Path) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    Path(store.materialize(source)).unlink()
    metadata = _metadata(store)

    def corrupt_references() -> set[str]:
        raise json.JSONDecodeError("corrupt references", "", 0)

    with pytest.raises(json.JSONDecodeError):
        store.collect(corrupt_references)
    assert metadata.exists()


@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_metadata_io_failures_protect_existing_bytes(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    metadata = _metadata(store)
    read_bytes = Path.read_bytes
    if failure == "missing":
        metadata.unlink()
    else:

        def fail_metadata_read(target):
            if target == metadata:
                raise PermissionError("metadata unreadable")
            return read_bytes(target)

        monkeypatch.setattr(Path, "read_bytes", fail_metadata_read)

    assert store.list_items() == []
    assert store.collect(set) == []
    with pytest.raises(OSError):
        store.remove(path)
    with pytest.raises(OSError):
        store.materialize(source)
    assert Path(path).read_bytes() == source.read_bytes()


def test_missing_metadata_cannot_create_a_second_object_for_another_extension(
    tmp_path: Path,
    source: Path,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = store.materialize(source)
    _metadata(store).unlink()
    renamed = source.with_suffix(".png")
    renamed.write_bytes(source.read_bytes())

    with pytest.raises(OSError):
        store.materialize(renamed)

    assert list(store.root.iterdir()) == [Path(path)]


def test_failed_unlink_keeps_metadata_and_allows_retry(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PublicationAssetStore(tmp_path / "assets")
    path = Path(store.materialize(source))
    unlink = os.unlink

    def fail_asset_unlink(target, *args, **kwargs):
        if Path(target).name == path.name:
            raise PermissionError("image is busy")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_asset_unlink)
        with pytest.raises(OSError, match="image is busy"):
            store.remove(path)

    assert len(store.list_items()) == 1
    assert store.remove(path)


def test_rejects_non_image_sources(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    store = PublicationAssetStore(tmp_path / "assets")

    with pytest.raises(ValueError):
        store.materialize(source)
