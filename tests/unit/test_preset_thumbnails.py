from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QImage
import pytest

from solin.core.scenes import preset_thumbnails
from solin.core.scenes.engine import camera_still_channel
from solin.core.scenes.preset_thumbnails import PresetThumbnailStore
from solin.widgets.scenes.camera_still import CameraStillCapture


def test_store_round_trips_and_deletes_a_preset_still(tmp_path: Path) -> None:
    store = PresetThumbnailStore(tmp_path / "stills")

    assert store.load("wide") is None
    assert store.save("wide", b"jpeg-bytes")
    assert store.load("wide") == b"jpeg-bytes"
    assert store.save("wide", b"newer")  # replaced, not appended
    assert store.load("wide") == b"newer"
    assert [path.name for path in (tmp_path / "stills").iterdir()] == ["wide.jpg"]

    store.delete("wide")
    store.delete("wide")  # already gone: fine
    assert store.load("wide") is None


@pytest.mark.parametrize("preset_id", ["", "..", "../escape", "a/b", "a\\b", "x" * 129])
def test_store_never_turns_an_unsafe_id_into_a_path(tmp_path: Path, preset_id: str) -> None:
    store = PresetThumbnailStore(tmp_path / "stills")

    assert not store.save(preset_id, b"data")
    assert store.load(preset_id) is None
    store.delete(preset_id)
    assert not (tmp_path / "escape.jpg").exists()


def test_store_refuses_oversized_stills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preset_thumbnails, "MAXIMUM_THUMBNAIL_BYTES", 8)
    store = PresetThumbnailStore(tmp_path)

    assert not store.save("wide", b"123456789")
    (tmp_path / "wide.jpg").write_bytes(b"123456789")
    assert store.load("wide") is None


class _Engine:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple] = []

    def set_thumbnail_egress(self, descriptor, scene_ids, width, height, *, channel_id):
        self.calls.append((channel_id, descriptor is not None, scene_ids, width, height))
        return self.ready


def _frame(color: int) -> QImage:
    image = QImage(320, 180, QImage.Format.Format_ARGB32)
    image.fill(color)
    return image


class _Feed(QObject):
    """The capture's feed without shared memory or a reader thread."""

    thumbnail_ready = Signal(str, QImage)

    def __init__(self, channel_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.channel_id = channel_id
        self.descriptor = None
        self.closed = False

    def reconfigure(self, scene_ids, width, height) -> bool:
        self.descriptor = object()
        return True

    def close(self) -> None:
        self.closed = True
        self.descriptor = None


def _capture(engine: _Engine, **timing) -> tuple[CameraStillCapture, list, list]:
    capture = CameraStillCapture(engine, "stage", feed_factory=_Feed, **timing)  # type: ignore[arg-type]
    captured: list = []
    failed: list = []
    capture.captured.connect(lambda camera, image: captured.append((camera, image)))
    capture.failed.connect(failed.append)
    return capture, captured, failed


def _wait(condition, timeout_ms: int = 2000) -> None:
    from PySide6.QtTest import QTest

    waited = 0
    while not condition() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    assert condition()


def test_capture_takes_the_settled_frame_and_closes_its_feed() -> None:
    engine = _Engine()
    capture, captured, failed = _capture(engine, settle_ms=50, timeout_ms=2000)

    capture.start()
    capture._on_frame("stage", _frame(0xFF000000))  # first frames: black, warming up
    capture._on_frame("other", _frame(0xFFFF0000))  # another camera: ignored
    capture._on_frame("stage", _frame(0xFF00FF00))
    _wait(lambda: capture.finished)

    assert engine.calls[0] == (camera_still_channel("stage"), True, ("stage",), 320, 180)
    assert engine.calls[-1][:2] == (camera_still_channel("stage"), False)  # closed again
    assert capture._feed.closed
    assert failed == []
    (camera, image), = captured
    assert camera == "stage" and image.pixelColor(10, 10).green() == 255


def test_capture_without_frames_fails_after_its_timeout() -> None:
    engine = _Engine()
    capture, captured, failed = _capture(engine, settle_ms=50, timeout_ms=80)

    capture.start()
    _wait(lambda: capture.finished)

    assert captured == [] and failed == ["stage"]
    assert engine.calls[-1][1] is False


def test_capture_fails_at_once_when_the_engine_cannot_hear_it() -> None:
    engine = _Engine(ready=False)
    capture, captured, failed = _capture(engine)

    capture.start()

    assert capture.finished and failed == ["stage"] and captured == []


def test_a_cancelled_capture_reports_nothing() -> None:
    engine = _Engine()
    capture, captured, failed = _capture(engine, settle_ms=50)
    capture.start()
    capture._on_frame("stage", _frame(0xFF00FF00))

    capture.cancel()
    QCoreApplication.processEvents()

    assert capture.finished and captured == [] and failed == []
    assert engine.calls[-1][1] is False


def test_store_encodes_and_decodes_a_still(tmp_path: Path) -> None:
    store = PresetThumbnailStore(tmp_path)
    image = QImage(64, 36, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)

    assert store.save_image("wide", image)
    loaded = store.load_image("wide")

    assert loaded is not None and (loaded.width(), loaded.height()) == (64, 36)
    assert loaded.pixelColor(10, 10).green() > 200
    assert not store.save_image("empty", QImage())
    assert store.load_image("missing") is None
