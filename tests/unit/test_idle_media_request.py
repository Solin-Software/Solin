from solin.core.projection.idle_media import (
    create_idle_media_request,
    existing_idle_media_path,
    supports_idle_media_source,
)


def test_idle_media_support_matches_preview_eligibility() -> None:
    assert supports_idle_media_source("video", "C:/media/idle.mp4") is True
    assert supports_idle_media_source("image", "C:/media/idle.png") is True
    assert supports_idle_media_source("audio", "C:/media/idle.mp3") is False
    assert supports_idle_media_source("video", "https://example.test/idle.mp4") is False
    assert supports_idle_media_source("image", "") is False


def test_idle_media_request_requires_an_existing_local_file(tmp_path) -> None:
    path = tmp_path / "idle.png"
    path.write_bytes(b"image")

    request = create_idle_media_request(
        title="Idle image",
        media_type="image",
        source=str(path),
    )

    assert request is not None
    assert request.title == "Idle image"
    assert request.media_type == "image"
    assert request.path == str(path)
    assert request.thumbnail_path == str(path)
    assert existing_idle_media_path("image", str(path)) == str(path)


def test_idle_media_request_keeps_an_existing_video_thumbnail(tmp_path) -> None:
    video = tmp_path / "idle.mp4"
    thumbnail = tmp_path / "idle.jpg"
    video.write_bytes(b"video")
    thumbnail.write_bytes(b"thumbnail")

    request = create_idle_media_request(
        title="Idle video",
        media_type="video",
        source=str(video),
        thumbnail_path=str(thumbnail),
    )

    assert request is not None
    assert request.thumbnail_path == str(thumbnail)


def test_idle_media_request_rejects_missing_remote_and_audio_sources(tmp_path) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    assert (
        create_idle_media_request(
            title="Missing",
            media_type="video",
            source=str(tmp_path / "missing.mp4"),
        )
        is None
    )
    assert (
        create_idle_media_request(
            title="Remote",
            media_type="video",
            source="https://example.test/video.mp4",
        )
        is None
    )
    assert (
        create_idle_media_request(
            title="Audio",
            media_type="audio",
            source=str(audio),
        )
        is None
    )
