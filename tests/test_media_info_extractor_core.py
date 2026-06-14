from __future__ import annotations

import struct
import zlib

from solin.widgets import media_info_extractor as media_info_module
from solin.widgets.media_info_extractor import (
    MediaInfoQueue,
    _audio_info_from_file,
    _embedded_image_is_complete,
    _id3v2_info_from_bytes,
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _large_valid_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00")
    padding = b"cover\x00" + (b"x" * (600 * 1024))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", padding)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def _syncsafe(value: int) -> bytes:
    return bytes(
        (
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        )
    )


def _mp3_with_cover(cover: bytes) -> bytes:
    apic_data = b"\x00image/png\x00\x03\x00" + cover
    frame = b"APIC" + struct.pack(">I", len(apic_data)) + b"\x00\x00" + apic_data
    return b"ID3\x03\x00\x00" + _syncsafe(len(frame)) + frame + b"\xff\xfb"


def test_audio_info_reads_complete_id3_tag_larger_than_default_prefix(tmp_path):
    cover = _large_valid_png()
    path = tmp_path / "large-cover.mp3"
    path.write_bytes(_mp3_with_cover(cover))

    extracted_cover, _title = _audio_info_from_file(str(path))

    assert len(cover) > 512 * 1024
    assert extracted_cover == cover
    assert _embedded_image_is_complete(extracted_cover)


def test_truncated_id3_frame_does_not_expose_partial_cover():
    data = _mp3_with_cover(_large_valid_png())

    cover, _title = _id3v2_info_from_bytes(data[: 512 * 1024])

    assert cover is None


def test_truncated_png_is_rejected_before_native_decoder():
    cover = _large_valid_png()

    assert _embedded_image_is_complete(cover)
    assert not _embedded_image_is_complete(cover[:-12])


def test_media_info_queue_ignores_extractors_from_cleared_generation(
    monkeypatch,
    tmp_path,
):
    class _Signal:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

        def emit(self, *args):
            assert self.callback is not None
            self.callback(*args)

    class _Extractor:
        def __init__(self):
            self.info_ready = _Signal()
            self.thumbnail_failed = _Signal()
            self.duration_ready = _Signal()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def deleteLater(self):
            pass

    extractors: list[_Extractor] = []

    def _factory(_index, _url, _media_type, _parent):
        extractor = _Extractor()
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr(media_info_module, "_create_extractor", _factory)
    queue = MediaInfoQueue(tmp_path / "media", tmp_path / "thumbs")

    queue.request(0, str(tmp_path / "first.mp4"))
    first = extractors[0]
    queue.clear()
    queue.request(0, str(tmp_path / "second.mp4"))

    assert first.cancelled is True
    assert queue._active[0].url.endswith("second.mp4")

    first.info_ready.emit(0, object(), "stale")
    first.thumbnail_failed.emit(0)
    first.duration_ready.emit(0, 90_000)

    assert queue._active[0].url.endswith("second.mp4")
    assert queue.get_cached(0) == (None, "")

    second = extractors[1]
    queue.invalidate(0)
    second.info_ready.emit(0, object(), "retired")

    assert second.cancelled is True
    assert queue.get_cached(0) == (None, "")
