import pytest

from solin.core.media.thumbnail_store import (
    ThumbnailStore,
    read_thumbnail_source_signature,
)


def test_thumbnail_store_saves_and_copies_atomically(tmp_path):
    store = ThumbnailStore(tmp_path / "thumbs")

    saved = store.save_bytes(
        "item-1",
        b"first",
        source_signature="5:10",
    )
    source = tmp_path / "source.jpg"
    source.write_bytes(b"second")
    copied = store.copy_from(
        "item-1",
        source,
        source_signature="6:20",
    )

    assert saved == copied == tmp_path / "thumbs" / "item-1.jpg"
    assert copied.read_bytes() == b"second"
    assert read_thumbnail_source_signature(copied) == "6:20"
    assert store.exists("item-1")
    assert list(store.root.glob(".*.tmp")) == []


def test_thumbnail_store_removes_stale_provenance_for_unbound_copy(tmp_path):
    store = ThumbnailStore(tmp_path / "thumbs")
    store.save_bytes("item-1", b"first", source_signature="5:10")

    source = tmp_path / "source.jpg"
    source.write_bytes(b"second")
    copied = store.copy_from("item-1", source)

    assert read_thumbnail_source_signature(copied) == ""
    assert not store.source_signature_path("item-1").exists()


@pytest.mark.parametrize("item_id", ["", "../escape", "folder/item"])
def test_thumbnail_store_rejects_invalid_item_ids(tmp_path, item_id):
    store = ThumbnailStore(tmp_path / "thumbs")

    with pytest.raises(ValueError, match="Invalid thumbnail"):
        store.path(item_id)
