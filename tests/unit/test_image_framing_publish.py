"""Zoom and pan must reach the projected frame.

Framing travels beside the pixels as a control, and under the libobs engine
nothing consumed it: the publisher's set_image_transform is a no-op, so an image
was shown unframed however the operator had zoomed or panned it. The framing is
now baked into the published frame, the way the retired native compositor did it.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from solin.controllers.content_frame_ingress_controller import _framed_for_canvas
from solin.core.projection.image_framing import ImageTransform


_APP = QApplication.instance() or QApplication([])
_CANVAS = QSize(400, 200)


def _image(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 255, 0))
    return image


def test_the_frame_is_canvas_sized() -> None:
    framed = _framed_for_canvas(_image(200, 100), ImageTransform(1.0, 0.0, 0.0), _CANVAS)

    assert framed.size() == _CANVAS


def test_an_unzoomed_image_is_letterboxed_not_stretched() -> None:
    """A 1:1 image on a 2:1 canvas keeps its shape, with empty bars beside it."""
    framed = _framed_for_canvas(_image(100, 100), ImageTransform(1.0, 0.0, 0.0), _CANVAS)

    assert framed.pixelColor(200, 100).green() == 255      # centre: the image
    assert framed.pixelColor(5, 100).alpha() == 0          # left bar: empty
    assert framed.pixelColor(395, 100).alpha() == 0        # right bar: empty


def test_zooming_in_fills_what_was_empty() -> None:
    tall = _image(100, 100)

    unzoomed = _framed_for_canvas(tall, ImageTransform(1.0, 0.0, 0.0), _CANVAS)
    zoomed = _framed_for_canvas(tall, ImageTransform(2.5, 0.0, 0.0), _CANVAS)

    assert unzoomed.pixelColor(5, 100).alpha() == 0
    assert zoomed.pixelColor(5, 100).green() == 255


def test_panning_moves_the_image() -> None:
    """Pan is a fraction of the canvas, matching the operator's own preview."""
    image = _image(100, 100)

    centred = _framed_for_canvas(image, ImageTransform(1.0, 0.0, 0.0), _CANVAS)
    panned = _framed_for_canvas(image, ImageTransform(1.0, 0.25, 0.0), _CANVAS)

    assert centred.pixelColor(340, 100).alpha() == 0    # right of the centred image
    assert panned.pixelColor(340, 100).green() == 255   # the pan brought it over


def test_the_image_never_escapes_the_canvas() -> None:
    """A big pan must not paint outside the frame the projector will show."""
    framed = _framed_for_canvas(_image(100, 100), ImageTransform(3.0, 5.0, 5.0), _CANVAS)

    assert framed.size() == _CANVAS  # clipped, not grown


def test_a_wide_image_on_a_wide_canvas_fills_it() -> None:
    framed = _framed_for_canvas(_image(200, 100), ImageTransform(1.0, 0.0, 0.0), _CANVAS)

    assert framed.pixelColor(2, 100).green() == 255
    assert framed.pixelColor(397, 100).green() == 255
