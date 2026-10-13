"""Raw media extraction pixels are validated and copied at the Qt boundary."""

from __future__ import annotations

import gc

import pytest
from PySide6.QtGui import QImage

import solin.ui.thumbnail_images as thumbnail_images
from solin.core.media.media_extract import MediaExtractImage
from solin.ui.thumbnail_images import media_extract_image_to_qimage


def _image(**changes):
    values = dict(data=b"\x33\x66\xcc\x80", width=1, height=1, stride=4, kind="cover")
    values.update(changes)
    return MediaExtractImage(**values)


@pytest.mark.parametrize("kind", ["cover", "frame"])
def test_native_bgra_colors_and_alpha_are_preserved(kind):
    source = _image(
        data=b"\x33\x66\xcc\x80\x00\xff\x00\xff",
        width=2,
        stride=8,
        kind=kind,
    )
    image = media_extract_image_to_qimage(source)
    assert image.format() == QImage.Format.Format_ARGB32
    assert (image.width(), image.height(), image.bytesPerLine()) == (2, 1, 8)
    assert image.pixelColor(0, 0).getRgb() == (204, 102, 51, 128)
    assert image.pixelColor(1, 0).getRgb() == (0, 255, 0, 255)


def test_rows_use_the_native_stride():
    source = _image(data=b"\x00\x00\xff\xff\xff\x00\x00\xff", height=2)
    image = media_extract_image_to_qimage(source)
    assert image.pixelColor(0, 0).getRgb() == (255, 0, 0, 255)
    assert image.pixelColor(0, 1).getRgb() == (0, 0, 255, 255)


def test_image_owns_pixels_after_source_storage_is_released(monkeypatch):
    views = []

    class BorrowedView:
        Format = QImage.Format

        def __new__(cls, *args):
            view = QImage(*args)
            views.append(view)
            return view

    monkeypatch.setattr(thumbnail_images, "QImage", BorrowedView)
    source = _image()
    image = media_extract_image_to_qimage(source)
    # A shallow QImage copy shares its cache key; an owned pixel copy does not.
    assert image.cacheKey() != views[0].cacheKey()
    views.clear()
    del source
    gc.collect()
    assert image.pixelColor(0, 0).getRgb() == (204, 102, 51, 128)


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 0},
        {"width": -1},
        {"height": 0},
        {"height": -1},
        {"width": True},
        {"height": 1.0},
        {"width": 2**31},
        {"height": 2**31},
        {"stride": 0},
        {"stride": -4},
        {"stride": 3},
        {"stride": 8},
        {"stride": True},
        {"data": b""},
        {"data": b"abc"},
        {"data": b"abcde"},
        {"data": "BGRA"},
        {"data": bytearray(4)},
    ],
)
def test_invalid_native_pixels_are_rejected_before_qt_reads_them(monkeypatch, changes):
    source = _image()
    # Simulate a malformed injected/native result bypassing domain validation.
    for name, value in changes.items():
        object.__setattr__(source, name, value)

    def forbidden_qimage(*args):
        pytest.fail("Invalid raw pixels reached QImage")

    monkeypatch.setattr(thumbnail_images, "QImage", forbidden_qimage)
    with pytest.raises(ValueError, match="geometry or buffer"):
        media_extract_image_to_qimage(source)


def test_qt_copy_failure_is_reported(monkeypatch):
    class NullCopy:
        Format = QImage.Format

        def __init__(self, *args):
            pass

        def copy(self):
            return QImage()

    monkeypatch.setattr(thumbnail_images, "QImage", NullCopy)
    with pytest.raises(ValueError, match="Could not copy"):
        media_extract_image_to_qimage(_image())
