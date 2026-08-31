from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from unittest.mock import patch

from solin.widgets.common.buffered_slider import BufferedSlider
from solin.ui.themed_tooltip import ThemedTooltipFilter


_APP = QApplication.instance() or QApplication([])


def test_disabled_buffered_slider_hides_handle_and_uses_arrow_cursor() -> None:
    slider = BufferedSlider()

    assert slider.findChild(ThemedTooltipFilter) is not None
    assert slider._should_draw_handle() is True
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor

    slider.setEnabled(False)

    assert slider._should_draw_handle() is False
    assert slider.cursor().shape() == Qt.CursorShape.ArrowCursor

    slider.setEnabled(True)

    assert slider._should_draw_handle() is True
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor
    slider.deleteLater()


def test_buffered_slider_repaints_only_when_visible_progress_changes() -> None:
    slider = BufferedSlider()
    slider.resize(500, slider.height())
    slider.setRange(0, 30_000)

    with patch.object(slider, "update") as update:
        slider.setValue(10)
        slider.setValue(50)
        assert slider.value() == 50
        update.assert_not_called()

        slider.setValue(60)
        update.assert_called_once_with()

        update.reset_mock()
        slider.setValue(70)
        assert slider.value() == 70
        update.assert_not_called()

    slider.deleteLater()
