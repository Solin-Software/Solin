from pathlib import Path

from solin.core.media.presentation_probe import (
    MediaPresentationProbeRequest,
    ProbedMediaAvailability,
    probe_media_presentation,
)
from solin.core.media.thumbnail_store import ThumbnailStore


def test_probe_resolves_local_media_and_thumbnail(tmp_path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    store = ThumbnailStore(tmp_path)
    thumbnail = store.save_bytes(
        "clip",
        b"image",
        source_signature="5:10",
    )

    result = probe_media_presentation(
        MediaPresentationProbeRequest(
            source=str(media),
            media_cache_dir=tmp_path / "cache",
            thumbnail_path=thumbnail,
        )
    )

    assert result.availability == ProbedMediaAvailability.AVAILABLE
    assert result.local_path == str(media.resolve())
    assert result.thumbnail_exists is True
    assert result.thumbnail_source_signature == "5:10"


def test_probe_distinguishes_missing_file_from_transient_os_error(tmp_path, monkeypatch) -> None:
    missing = probe_media_presentation(
        MediaPresentationProbeRequest(
            source=str(tmp_path / "missing.mp4"),
            media_cache_dir=tmp_path / "cache",
        )
    )
    assert missing.availability == ProbedMediaAvailability.MISSING

    def locked(_path):
        raise PermissionError("cloud provider lock")

    monkeypatch.setattr("solin.core.media.presentation_probe.os.stat", locked)
    locked_result = probe_media_presentation(
        MediaPresentationProbeRequest(
            source=str(tmp_path / "locked.mp4"),
            media_cache_dir=tmp_path / "cache",
        )
    )
    assert locked_result.availability == ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE
    assert "cloud provider lock" in locked_result.error


def test_remote_probe_uses_completed_cache_without_requiring_it(tmp_path) -> None:
    result = probe_media_presentation(
        MediaPresentationProbeRequest(
            source="https://example.test/video.mp4",
            media_cache_dir=Path(tmp_path / "cache"),
        )
    )

    assert result.availability == ProbedMediaAvailability.AVAILABLE
    assert result.cached is False
    assert result.local_path == ""
