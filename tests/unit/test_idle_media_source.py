"""Unit tests for the poster-based :class:`IdleMediaSource`.

QtMultimedia was removed, so the idle source no longer runs a ``QMediaPlayer``.
An image loads directly into a :class:`QImage`; a video is represented by a
single poster frame extracted asynchronously with ffmpeg. These tests exercise
the real object (an offscreen ``QGuiApplication`` is enough) and drive the async
poster path by calling :meth:`IdleMediaSource._on_poster_ready` directly.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication, QImage

from solin.projection.idle_source import IdleMediaSource


_APP = QGuiApplication.instance() or QGuiApplication([])


def _png_bytes(width: int = 4, height: int = 4, color: int = 0xFF3366CC) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def test_image_media_emits_a_single_frame(tmp_path):
    path = tmp_path / "idle.png"
    path.write_bytes(_png_bytes())
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)

    source.set_media(str(path))

    assert source.media_type == "image"
    assert len(frames) == 1
    assert isinstance(frames[0], QImage)
    assert not frames[0].isNull()
    assert source.current_image is not None


def test_video_media_defers_the_poster_frame(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: calls.append((path, generation)),
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)

    source.set_media("/tmp/clip.mp4")

    # Video media does not paint synchronously — the poster is extracted async.
    assert source.media_type == "video"
    assert frames == []
    assert source.current_image is None
    assert calls == [("/tmp/clip.mp4", source._generation)]


def test_poster_ready_publishes_the_extracted_frame(monkeypatch):
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: None,
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)
    source.set_media("/tmp/clip.mp4")

    source._on_poster_ready(source._generation, _png_bytes())

    assert len(frames) == 1
    assert not frames[0].isNull()
    assert source.current_image is not None


def test_stale_poster_is_ignored(monkeypatch):
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: None,
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)
    source.set_media("/tmp/clip.mp4")

    # A poster from a superseded generation must never reach the surfaces.
    source._on_poster_ready(source._generation - 1, _png_bytes())

    assert frames == []
    assert source.current_image is None


def test_empty_poster_payload_is_ignored(monkeypatch):
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: None,
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)
    source.set_media("/tmp/clip.mp4")

    source._on_poster_ready(source._generation, None)

    assert frames == []
    assert source.current_image is None


def test_set_playing_is_a_noop(monkeypatch):
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: None,
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)
    source.set_media("/tmp/clip.mp4")

    # There is no live decoder to gate — set_playing must do nothing.
    source.set_playing(True)
    source.set_playing(False)

    assert frames == []


def test_clear_forgets_the_current_media(tmp_path):
    path = tmp_path / "idle.png"
    path.write_bytes(_png_bytes())
    source = IdleMediaSource()
    source.set_media(str(path))
    assert source.current_image is not None

    source.clear()

    assert source.media_type == ""
    assert source.current_image is None


def test_cleanup_invalidates_pending_posters(monkeypatch):
    monkeypatch.setattr(
        IdleMediaSource,
        "_extract_poster",
        lambda self, path, generation: None,
    )
    source = IdleMediaSource()
    frames: list[QImage] = []
    source.frame_ready.connect(frames.append)
    source.set_media("/tmp/clip.mp4")
    pending_generation = source._generation

    source.cleanup()
    # A poster that finishes after cleanup belongs to an old generation.
    source._on_poster_ready(pending_generation, _png_bytes())

    assert frames == []
