from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal, qInstallMessageHandler
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from solin.core.projection.image_framing import cover_zoom_for_frame
from solin.core.projection.image_framing import ImageTransform
from solin.projection.window import FloatingPreviewWindow
from solin.widgets.projection.preview import ImagePreviewWidget


_APP = QApplication.instance() or QApplication([])


class _FontManager(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        pass

    def family(self, _name: str) -> str:
        return "Arial"


def _pixmap(width: int = 300, height: int = 400) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#ffffff"))
    return pixmap


def _move_mouse(widget, position: QPointF) -> None:
    _send_mouse(
        widget,
        QEvent.Type.MouseMove,
        position,
        button=Qt.MouseButton.NoButton,
        buttons=Qt.MouseButton.NoButton,
    )


def _send_mouse(
    widget,
    event_type: QEvent.Type,
    position: QPointF,
    *,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> None:
    global_position = QPointF(widget.mapToGlobal(position.toPoint()))
    QApplication.sendEvent(
        widget,
        QMouseEvent(
            event_type,
            position,
            global_position,
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        ),
    )


def test_native_floating_preview_cursor_returns_to_arrow_away_from_edges() -> None:
    window = FloatingPreviewWindow(_FontManager())
    try:
        window.resize(640, 360)
        window.set_native_output_active(True)
        _APP.processEvents()
        surface = window.native_video_surface
        overlay = surface.input_overlay
        assert overlay is not None
        assert overlay.isVisible()
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        if sys.platform == "win32":
            assert not overlay.testAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground
            )
            assert not overlay.updatesEnabled()
        center_y = surface.height() / 2

        _move_mouse(overlay, QPointF(1, center_y))

        assert window.cursor().shape() == Qt.CursorShape.SizeHorCursor
        assert surface.cursor().shape() == Qt.CursorShape.SizeHorCursor
        assert overlay.cursor().shape() == Qt.CursorShape.SizeHorCursor

        _move_mouse(overlay, QPointF(surface.width() / 2, center_y))

        assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert surface.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert overlay.cursor().shape() == Qt.CursorShape.ArrowCursor
    finally:
        window.close()
        window.deleteLater()
        _APP.processEvents()


def test_native_floating_preview_overlay_forwards_resize_drag() -> None:
    window = FloatingPreviewWindow(_FontManager())
    try:
        window.resize(640, 360)
        window.set_native_output_active(True)
        _APP.processEvents()
        overlay = window.native_video_surface.input_overlay
        assert overlay is not None
        center_y = overlay.height() / 2
        original = window.geometry()

        _send_mouse(
            overlay,
            QEvent.Type.MouseButtonPress,
            QPointF(1, center_y),
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        _send_mouse(
            overlay,
            QEvent.Type.MouseMove,
            QPointF(-31, center_y),
            button=Qt.MouseButton.NoButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        _send_mouse(
            overlay,
            QEvent.Type.MouseButtonRelease,
            QPointF(1, center_y),
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.NoButton,
        )

        assert window.width() > original.width()
        assert window.x() < original.x()
        assert window._resize_dir == (False, False, False, False)
    finally:
        window.close()
        window.deleteLater()
        _APP.processEvents()


def test_preview_theme_styles_do_not_emit_qss_parse_warnings() -> None:
    messages: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _mode, _context, message: messages.append(message)
    )
    try:
        widget = ImagePreviewWidget()
        widget.apply_theme()
        _APP.processEvents()
    finally:
        qInstallMessageHandler(previous_handler)

    stylesheet_errors = [
        message for message in messages if "parse stylesheet" in message.lower()
    ]
    assert stylesheet_errors == []


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


def test_loading_prepared_transform_does_not_change_framing_pill_state() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 800)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap())
    widget.configure_framing(
        match_projection_aspect=False,
        constrain_to_frame=False,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )

    applied = widget.set_current_transform(ImageTransform(1.4, 0.15, -0.2))

    assert applied == ImageTransform(1.4, 0.15, -0.2)
    assert not widget._aspect_btn.isChecked()
    assert not widget._bounds_btn.isChecked()


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


def test_projection_aspect_keeps_tool_pill_anchored_to_preview() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 800)
    widget.show()
    _APP.processEvents()
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap())
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=False,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )

    bar = widget._action_bar
    assert bar.isVisible()
    assert bar.y() == widget.height() - bar.height() - 18


def test_set_pixmap_repositions_visible_tool_pill_after_late_layout_resize() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1000, 360)
    widget.show()
    _APP.processEvents()
    widget.set_image_mode(True)
    pixmap = _pixmap()
    widget.set_image_pixmap_fresh(pixmap)
    old_y = widget._action_bar.y()

    widget.resize(1000, 800)
    widget.setPixmap(pixmap)

    bar = widget._action_bar
    assert old_y != bar.y()
    assert bar.y() == widget.height() - bar.height() - 18


def test_constrained_zoom_snaps_to_exact_frame_cover_threshold() -> None:
    widget = ImagePreviewWidget()
    widget.resize(1600, 900)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap(900, 1600))
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=True,
        aspect_ratio=16 / 9,
        aspect_ratio_label="16:9",
    )
    cover_zoom = cover_zoom_for_frame(900, 1600, 1600, 900)

    widget._zoom = cover_zoom - 0.02
    widget._apply_zoom_factor(1.15)
    snapped_zoom = widget.current_transform().zoom
    widget._apply_zoom_factor(1.15)

    assert snapped_zoom == cover_zoom
    assert widget.current_transform().zoom > cover_zoom


def test_constrained_zoom_snaps_to_exact_cover_threshold_in_tall_frame() -> None:
    widget = ImagePreviewWidget()
    widget.resize(900, 1600)
    widget.set_image_mode(True)
    widget.set_image_pixmap_fresh(_pixmap(1600, 900))
    widget.configure_framing(
        match_projection_aspect=True,
        constrain_to_frame=True,
        aspect_ratio=9 / 16,
        aspect_ratio_label="9:16",
    )
    cover_zoom = cover_zoom_for_frame(1600, 900, 900, 1600)

    widget._zoom = cover_zoom - 0.02
    widget._apply_zoom_factor(1.15)
    snapped_zoom = widget.current_transform().zoom
    widget._apply_zoom_factor(1.15)

    assert snapped_zoom == cover_zoom
    assert widget.current_transform().zoom > cover_zoom
