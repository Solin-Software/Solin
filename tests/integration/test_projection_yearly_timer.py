from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout

from solin.core.timer.models import MediaCountdownPresentation
from solin.core.projection.image_framing import IDENTITY_IMAGE_TRANSFORM
from solin.projection.window import BaseProjectionView
from solin.projection.yearly_text import YearlyTextWidget


_APP = QApplication.instance() or QApplication([])


class _FontManagerStub(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        return None

    def family(self, _name: str) -> str:
        return "Georgia"


def _render_widget(quote: str, reference: str) -> YearlyTextWidget:
    widget = YearlyTextWidget(_FontManagerStub())
    widget.resize(1280, 720)
    widget.set_text(quote, reference)
    widget.show()
    _APP.processEvents()
    return widget


class _ProjectionViewHarness(BaseProjectionView):
    def __init__(self) -> None:
        super().__init__()
        self._font_manager = _FontManagerStub()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._build_projection_stack(layout)


class _UpdateTrackingYearlyTextWidget(YearlyTextWidget):
    def __init__(self) -> None:
        self.update_requests = 0
        super().__init__(_FontManagerStub())

    def update(self, *args) -> None:
        self.update_requests += 1
        super().update(*args)


def test_countdown_does_not_move_or_resize_non_colliding_yearly_text():
    widget = _render_widget(
        "Happy are those conscious of their spiritual need.",
        "Matthew 5:3",
    )
    original = widget.grab().toImage()

    widget.set_countdown(55, 90)
    _APP.processEvents()
    with_countdown = widget.grab().toImage()
    countdown_layout = widget._countdown_layout()
    assert countdown_layout is not None
    safe_bottom = int(countdown_layout.text_rect.top()) - max(
        8,
        int(widget.height() * 0.025),
    )
    byte_count = original.bytesPerLine() * safe_bottom

    assert original.constBits()[:byte_count].tobytes() == (
        with_countdown.constBits()[:byte_count].tobytes()
    )


def test_countdown_reserves_a_clean_gap_when_yearly_text_would_collide():
    widget = _render_widget(
        "\n".join(f"Annual text line {line}" for line in range(1, 10)),
        "Reference",
    )

    widget.set_countdown(55, 90)
    _APP.processEvents()
    image = widget.grab().toImage()
    countdown_layout = widget._countdown_layout()
    assert countdown_layout is not None
    countdown_top = int(countdown_layout.text_rect.top())
    safe_bottom = countdown_top - max(8, int(widget.height() * 0.025))

    for y in range(safe_bottom, countdown_top):
        for x in range(int(widget.width() * 0.2), int(widget.width() * 0.8)):
            assert image.pixelColor(x, y).lightness() <= 8


def test_countdown_digits_align_vertically_with_the_jw_badge():
    widget = _render_widget("Annual text", "Reference")
    widget.set_countdown(55, 90)

    countdown_layout = widget._countdown_layout()
    assert countdown_layout is not None
    _, badge_y, badge_size = widget._jw_badge_geometry()

    assert countdown_layout.text_rect.center().y() == badge_y + (badge_size / 2.0)


def test_clearing_yearly_timer_fades_whole_page_before_restoring_idle():
    view = _ProjectionViewHarness()
    view.resize(1280, 720)
    view.set_yearly_text("Annual text", "Reference")
    view.show()
    view._timer_presentation = MediaCountdownPresentation.YEARLY_TEXT
    view._yearly_widget.set_countdown(55, 90)
    view._stack.setCurrentIndex(view._PAGE_YEARLY)

    view.clear()

    assert view._stack.currentIndex() == view._PAGE_YEARLY
    assert view._yearly_widget._countdown_remaining == 55
    assert view._yearly_timer_exit_pending is True
    assert view._yearly_anim.duration() == view._YEARLY_TIMER_EXIT_FADE_DURATION_MS

    QTest.qWait((view._yearly_anim.duration() // 2) + 20)
    assert 0.0 < view._yearly_opacity.opacity() < 1.0
    assert view._yearly_widget._countdown_remaining == 55

    QTest.qWait((view._yearly_anim.duration() // 2) + 40)
    assert view._yearly_timer_exit_pending is False
    assert view._yearly_widget._countdown_remaining is None
    assert view._stack.currentIndex() == view._PAGE_YEARLY
    assert 0.0 <= view._yearly_opacity.opacity() < 1.0
    assert view._yearly_anim.duration() == view._YEARLY_FADE_IN_DURATION_MS

    QTest.qWait(view._yearly_anim.duration() + 40)
    assert view._yearly_opacity.opacity() == 1.0


def test_immediate_countdown_clear_always_requests_a_repaint():
    widget = _UpdateTrackingYearlyTextWidget()
    widget.set_countdown(55, 90)
    widget.update_requests = 0

    widget.clear_countdown()

    assert widget._countdown_remaining is None
    assert widget.update_requests >= 1


def test_new_projection_cancels_pending_yearly_page_fade_out():
    view = _ProjectionViewHarness()
    view.resize(1280, 720)
    view.set_yearly_text("Annual text", "Reference")
    view._has_idle_media = True
    view._timer_presentation = MediaCountdownPresentation.YEARLY_TEXT
    view._yearly_widget.set_countdown(55, 90)
    view._stack.setCurrentIndex(view._PAGE_YEARLY)

    view.clear()
    assert view._yearly_timer_exit_pending is True
    view.begin_video()
    QTest.qWait(view._yearly_anim.duration() + 40)

    assert view._yearly_timer_exit_pending is False
    assert view._accept_video_frames is True
    assert view._yearly_widget._countdown_remaining is None
    assert view._yearly_opacity.opacity() == 1.0
    assert view._stack.currentIndex() != view._PAGE_IDLE_MEDIA


def test_first_video_frame_switches_qt_projection_to_media_page():
    """The playback pre-roll must not mark video visible before a frame exists."""
    view = _ProjectionViewHarness()

    view.begin_video()

    assert view._is_showing_media is False

    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    view.update_frame(QVideoFrame(image))

    assert view._is_showing_media is True
    assert view._stack.currentIndex() == view._PAGE_MEDIA


def test_native_video_output_bypasses_qt_frame_materialization() -> None:
    view = _ProjectionViewHarness()
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    view.begin_video()

    view.set_native_output_active(True)
    view.update_frame(QVideoFrame(image))

    assert view._native_output_active
    assert view._stack.currentIndex() == view._PAGE_MEDIA
    assert view.display_label._video_frame is None

    view.clear()
    assert not view._native_output_active


def test_new_untransformed_image_resets_zoom_after_immediate_clear():
    view = _ProjectionViewHarness()
    image = QImage(160, 90, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    view.show_image_from_qimage(image)
    view.set_image_transform(2.0, 0.2, -0.1, animate=False)

    view.clear()
    view.show_image_from_qimage(image, cache_pixmap=False)

    assert view.display_label._image_transform.current == IDENTITY_IMAGE_TRANSFORM


def test_media_changes_do_not_install_or_run_a_cpu_opacity_fade() -> None:
    view = _ProjectionViewHarness()
    image = QImage(160, 90, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))

    view.show_image_from_qimage(image)

    assert view._stack.currentIndex() == view._PAGE_MEDIA
    assert view.display_label.graphicsEffect() is None
    assert not hasattr(view.display_label, "_fade_timer")

    view.clear()

    assert not view._is_showing_media
    assert view._stack.currentIndex() == view._PAGE_YEARLY
