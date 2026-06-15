import pytest

from solin.core.media.thumbnail_store import ThumbnailStore


def test_thumbnail_store_saves_and_copies_atomically(tmp_path):
    store = ThumbnailStore(tmp_path / "thumbs")

    saved = store.save_bytes("item-1", b"first")
    source = tmp_path / "source.jpg"
    source.write_bytes(b"second")
    copied = store.copy_from("item-1", source)

    assert saved == copied == tmp_path / "thumbs" / "item-1.jpg"
    assert copied.read_bytes() == b"second"
    assert store.exists("item-1")
    assert list(store.root.glob(".*.tmp")) == []


@pytest.mark.parametrize("item_id", ["", "../escape", "folder/item"])
def test_thumbnail_store_rejects_invalid_item_ids(tmp_path, item_id):
    store = ThumbnailStore(tmp_path / "thumbs")

    with pytest.raises(ValueError, match="Invalid thumbnail"):
        store.path(item_id)
