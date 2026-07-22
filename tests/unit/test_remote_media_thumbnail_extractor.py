from __future__ import annotations

import asyncio
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Signal
import pytest

from solin.controllers import remote_media_thumbnail_extractor as extractor_module
from solin.controllers.remote_media_thumbnail_extractor import (
    RemoteMediaThumbnailExtractor,
)
from solin.core.media.formats import MediaKind


_APP = QCoreApplication.instance() or QCoreApplication([])


class _Queue(QObject):
    info_ready = Signal(int, object, str)
    request_failed = Signal(int, object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.requests: list[tuple[int, str, str]] = []
        self.invalidated: list[int] = []
        self.stopped = False

    def request(self, index: int, location: str, media_type: str) -> None:
        self.requests.append((index, location, media_type))

    def shutdown(self) -> None:
        self.stopped = True

    def invalidate(self, index: int) -> None:
        self.invalidated.append(index)


class _Pixmap:
    def __init__(self, *, null: bool = False, width: int = 1_280, height: int = 720) -> None:
        self._null = null
        self._width = width
        self._height = height
        self.scaled_to: tuple[Any, ...] | None = None

    def isNull(self) -> bool:
        return self._null

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height

    def scaled(self, *values: Any) -> _Pixmap:
        self.scaled_to = values
        return self


def _extractor() -> tuple[RemoteMediaThumbnailExtractor, _Queue]:
    queue: _Queue | None = None

    def factory(parent: QObject) -> _Queue:
        nonlocal queue
        queue = _Queue(parent)
        return queue

    extractor = RemoteMediaThumbnailExtractor(factory, _APP)
    assert queue is not None
    return extractor, queue


def test_concurrent_requests_for_the_same_media_share_qt_extraction(monkeypatch) -> None:
    extractor, queue = _extractor()
    monkeypatch.setattr(extractor_module, "pixmap_to_jpeg_bytes", lambda _pixmap: b"jpeg")

    async def scenario() -> tuple[bytes | None, bytes | None]:
        first = asyncio.create_task(extractor.extract("C:/private/clip.mp4", MediaKind.VIDEO))
        second = asyncio.create_task(extractor.extract("C:/private/clip.mp4", MediaKind.VIDEO))
        await asyncio.sleep(0)
        assert queue.requests == [(0, "C:/private/clip.mp4", "video")]
        pixmap = _Pixmap()
        queue.info_ready.emit(0, pixmap, "")
        assert pixmap.scaled_to is not None
        assert pixmap.scaled_to[:2] == (640, 360)
        return await asyncio.gather(first, second)

    assert asyncio.run(scenario()) == [b"jpeg", b"jpeg"]
    extractor.shutdown()


def test_shutdown_resolves_pending_requests_without_leaking_qt_work() -> None:
    extractor, queue = _extractor()

    async def scenario() -> bytes | None:
        pending = asyncio.create_task(extractor.extract("C:/private/song.mp3", MediaKind.AUDIO))
        await asyncio.sleep(0)
        extractor.shutdown()
        return await pending

    assert asyncio.run(scenario()) is None
    assert queue.stopped is True


def test_cancelled_request_cancels_owned_qt_queue_work() -> None:
    extractor, queue = _extractor()

    async def scenario() -> None:
        pending = asyncio.create_task(extractor.extract("C:/private/clip.mp4", MediaKind.VIDEO))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert queue.invalidated == [0]
    extractor.shutdown()


def test_remote_image_uses_the_bounded_qt_media_queue(monkeypatch) -> None:
    extractor, queue = _extractor()
    monkeypatch.setattr(extractor_module, "pixmap_to_jpeg_bytes", lambda _pixmap: b"jpeg")

    async def scenario() -> bytes | None:
        pending = asyncio.create_task(
            extractor.extract("https://example.test/photo.png", MediaKind.IMAGE)
        )
        await asyncio.sleep(0)
        queue.info_ready.emit(0, _Pixmap(width=320, height=180), "")
        return await pending

    assert asyncio.run(scenario()) == b"jpeg"
    assert queue.requests == [(0, "https://example.test/photo.png", "image")]
    extractor.shutdown()


def test_unknown_media_does_not_enter_qt_queue() -> None:
    extractor, queue = _extractor()

    assert asyncio.run(extractor.extract("C:/private/file.bin", MediaKind.UNKNOWN)) is None
    assert queue.requests == []
    extractor.shutdown()
