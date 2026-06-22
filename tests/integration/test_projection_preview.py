from __future__ import annotations

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from solin.widgets.projection.preview import ImagePreviewWidget


_APP = QApplication.instance() or QApplication([])


def _pixmap(width: int = 300, height: int = 400) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#ffffff"))
    return pixmap


def test_preview_framing_buttons_update_tooltips_and_state() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 800)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap())

    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=True,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )

    assert widget._aspect_btn.isChecked()
    assert widget._bounds_btn.isChecked()
    assert widget._bounds_btn.isEnabled()
    assert "16:9" in widget._aspect_btn.toolTip()

    widget.configure_framing(
        match_projection_aspect=False,
        constrain_to_frame=True,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )

    assert not widget._aspect_btn.isChecked()
    assert not widget._bounds_btn.isChecked()
    assert not widget._bounds_btn.isEnabled()
    assert widget._bounds_btn.toolTip() == widget.tr("Enable projection aspect first")


def test_preview_language_change_refreshes_framing_tooltips() -> None:
    widget = ImagePreviewWidget()
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=False,
        aspect_ratio=16 / 10,
        aspect_ratio_label="16:10",
    )

    QApplication.sendEvent(widget, QEvent(QEvent.Type.LanguageChange))

    assert "16:10" in widget._aspect_btn.toolTip()


def test_preview_apply_emits_current_normalized_transform() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 800)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap())
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=False,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )
    emitted: list[tuple[float, float, float]] = []
    widget.apply_transform.connect(
        lambda zoom, x, y: emitted.append((zoom, x, y))
    )

    widget._zoom = 1.4
    widget._norm_x = 0.15
    widget._norm_y = -0.2
    widget._apply_btn.click()

    assert emitted == [(1.4, 0.15, -0.2)]


def test_preview_reset_keeps_original_image_fitted_when_constrained() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 800)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap())
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=True,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )

    transform = widget.reset_to_initial_transform()

    assert transform.zoom == 1.0
    assert transform.norm_x == 0.0
    assert transform.norm_y == 0.0


def test_projection_aspect_clips_pixels_outside_frame() -> None:
    widget = ImagePreviewWidget()
    widget.resize(100, 100)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap(160, 90))
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=False,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )
    widget._zoom = 2.0

    rendered = QPixmap(widget.size())
    widget.render(rendered)
    image = rendered.toImage()

    assert image.pixelColor(50, 5) == QColor("#0d1117")
