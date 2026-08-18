from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from solin.widgets.common.buffered_slider import BufferedSlider


_APP = QApplication.instance() or QApplication([])


def test_disabled_buffered_slider_hides_handle_and_uses_arrow_cursor() -> None:
    slider = BufferedSlider()

    assert slider._solin_themed_tooltip_filter is not None
    assert slider._should_draw_handle() is True
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor

    slider.setEnabled(False)

    assert slider._should_draw_handle() is False
    assert slider.cursor().shape() == Qt.CursorShape.ArrowCursor

    slider.setEnabled(True)

    assert slider._should_draw_handle() is True
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor
    slider.deleteLater()
