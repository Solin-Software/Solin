from __future__ import annotations

from io import BytesIO

from PIL import Image
import pytest

from solin.core.media.formats import MediaKind
from solin.core.remote_control.thumbnails import (
    private_media_thumbnail_source,
    render_image_thumbnail_bytes,
    render_media_placeholder_thumbnail,
)


@pytest.mark.parametrize(
    ("raw", "expected_kind"),
    (
        ({"url": "C:/private/photo.png", "type": "image"}, MediaKind.IMAGE),
        ({"url": "C:/private/clip.mp4", "type": "video"}, MediaKind.VIDEO),
        ({"url": "C:/private/song.mp3", "type": "audio"}, MediaKind.AUDIO),
        (
            {
                "media_ref": {
                    "file_path": "C:/private/song.bin",
                    "mime_type": "audio/mpeg",
                }
            },
            MediaKind.AUDIO,
        ),
    ),
)
def test_private_media_source_classifies_supported_media(raw, expected_kind) -> None:
    source = private_media_thumbnail_source(raw)

    assert source is not None
    assert source.extraction_kind is expected_kind
    assert source.placeholder_kind is expected_kind
    assert source.is_remote is False


def test_private_media_source_accepts_stored_https_without_exposing_it() -> None:
    source = private_media_thumbnail_source({"url": "https://example.test/video.mp4"})

    assert source is not None
    assert source.extraction_kind is MediaKind.VIDEO
    assert source.placeholder_kind is MediaKind.VIDEO
    assert source.is_remote is True


def test_private_media_source_prefers_resolved_artwork_for_meeting_video() -> None:
    source = private_media_thumbnail_source(
        {
            "media_type": "video",
            "resolved_url": "https://media.example.test/video.mp4",
            "thumbnail_local_path": "C:/stale/missing-thumb.jpg",
            "thumbnail_url": "https://media.example.test/thumb.jpg",
            "media_ref": {"mime_type": "video/mp4"},
        }
    )

    assert source is not None
    assert source.location == "https://media.example.test/thumb.jpg"
    assert source.extraction_kind is MediaKind.IMAGE
    assert source.placeholder_kind is MediaKind.VIDEO
    assert source.is_remote is True


def test_private_media_source_uses_resolved_video_when_artwork_is_absent() -> None:
    source = private_media_thumbnail_source(
        {
            "media_type": "video",
            "resolved_url": "https://media.example.test/video.mp4",
            "media_ref": {"mime_type": "video/mp4"},
        }
    )

    assert source is not None
    assert source.location == "https://media.example.test/video.mp4"
    assert source.extraction_kind is MediaKind.VIDEO
    assert source.placeholder_kind is MediaKind.VIDEO


def test_private_media_source_rejects_unknown_and_unsupported_schemes() -> None:
    assert private_media_thumbnail_source({"url": "C:/private/document.bin"}) is None
    assert private_media_thumbnail_source({"url": "file:///private/video.mp4"}) is None


@pytest.mark.parametrize("media_kind", (MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO))
def test_media_placeholder_is_a_bounded_deterministic_jpeg(media_kind: MediaKind) -> None:
    first = render_media_placeholder_thumbnail(media_kind)
    second = render_media_placeholder_thumbnail(media_kind)

    assert first == second
    assert first is not None and first.startswith(b"\xff\xd8")
    with Image.open(BytesIO(first)) as image:
        assert image.format == "JPEG"
        assert image.size == (640, 360)


def test_persisted_collection_cover_is_normalized_to_bounded_jpeg() -> None:
    source = BytesIO()
    Image.new("RGBA", (800, 1_200), (34, 139, 230, 180)).save(source, "PNG")

    result = render_image_thumbnail_bytes(source.getvalue())

    assert result is not None and result.startswith(b"\xff\xd8")
    with Image.open(BytesIO(result)) as image:
        assert image.format == "JPEG"
        assert image.width <= 640
        assert image.height <= 360


def test_invalid_collection_cover_is_rejected() -> None:
    assert render_image_thumbnail_bytes(b"not-an-image") is None
