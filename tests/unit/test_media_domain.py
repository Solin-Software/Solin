from __future__ import annotations

from solin.core.media.formats import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    MediaKind,
    media_kind_from_extension,
    media_kind_from_mime,
    media_kind_from_path,
    media_type_from_path,
    mime_to_ext,
)


def test_media_extension_groups_are_disjoint():
    assert VIDEO_EXTS.isdisjoint(AUDIO_EXTS)
    assert VIDEO_EXTS.isdisjoint(IMAGE_EXTS)
    assert AUDIO_EXTS.isdisjoint(IMAGE_EXTS)


def test_media_kind_from_extension_normalizes_case_dot_and_whitespace():
    assert media_kind_from_extension(" MP4 ") is MediaKind.VIDEO
    assert media_kind_from_extension(".Mp3") is MediaKind.AUDIO
    assert media_kind_from_extension("PNG") is MediaKind.IMAGE
    assert media_kind_from_extension(".bin") is MediaKind.UNKNOWN


def test_media_kind_from_path_ignores_url_query_and_fragment():
    assert (
        media_kind_from_path("https://cdn.example/media/clip.MP4?token=1#play") is MediaKind.VIDEO
    )
    assert media_kind_from_path("C:/Media/cover.JPEG") is MediaKind.IMAGE


def test_media_type_from_path_uses_explicit_unknown_default():
    assert media_type_from_path("archive.zip") == "unknown"
    assert media_type_from_path("", default="video") == "video"


def test_mime_classification_and_extension_ignore_parameters():
    assert media_kind_from_mime(" audio/mpeg; charset=binary ") is MediaKind.AUDIO
    assert mime_to_ext("video/quicktime; codecs=hvc1") == ".mov"
    assert mime_to_ext(None) == ".jpg"
    assert mime_to_ext("application/octet-stream") == ".jpg"
