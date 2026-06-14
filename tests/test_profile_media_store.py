from pathlib import Path

import pytest

from solin.core.media.profile_store import ProfileMediaStore


def test_profile_media_store_persists_embedded_media_atomically(tmp_path):
    store = ProfileMediaStore(tmp_path / "embedded", tmp_path / "images")

    path = store.save_embedded(
        b"content",
        "clip.MP4",
        identifier="item/unsafe",
    )

    result = Path(path)
    assert result == tmp_path / "embedded" / "item_unsafe.mp4"
    assert result.read_bytes() == b"content"
    assert list(result.parent.glob(".*.tmp")) == []


def test_profile_media_store_uses_safe_default_for_invalid_suffix(tmp_path):
    store = ProfileMediaStore(tmp_path / "embedded", tmp_path / "images")

    path = store.save_embedded(b"content", "media.bad-extension-too-long")

    assert Path(path).suffix == ".mp4"


def test_profile_media_store_persists_projected_images_as_png(tmp_path):
    store = ProfileMediaStore(tmp_path / "embedded", tmp_path / "images")

    path = store.save_projected_image(b"png")

    result = Path(path)
    assert result.parent == tmp_path / "images"
    assert result.suffix == ".png"
    assert result.read_bytes() == b"png"


def test_profile_media_store_rejects_invalid_fallback_suffix(tmp_path):
    store = ProfileMediaStore(tmp_path / "embedded", tmp_path / "images")

    with pytest.raises(ValueError, match="Invalid fallback"):
        store.save_embedded(b"content", "media", default_suffix="invalid")
