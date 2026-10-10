from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout

from solin.core.timer.models import MediaCountdownPresentation
from solin.core.projection.image_framing import IDENTITY_IMAGE_TRANSFORM
from solin.projection.window import BaseProjectionView
from solin.projection.yearly_text import YearlyTextWidget
from tests._qt import dispose_widget


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


def test_clearing_yearly_timer_fades_whole_page_before_restoring_idle(request):
    view = _ProjectionViewHarness()
    request.addfinalizer(lambda: dispose_widget(view))
    view.resize(1280, 720)
    view.set_yearly_text("Annual text", "Reference")
    view.show()
    view._timer_presentation = MediaCountdownPresentation.YEARLY_TEXT
    view._yearly_widget.set_countdown(55, 90)
    view._stack.setCurrentIndex(view._PAGE_YEARLY)

    def snapshot():
        return (
            view._yearly_timer_exit_pending,
            view._yearly_widget._countdown_remaining,
            view._stack.currentIndex(),
            view._yearly_opacity.opacity(),
            view._yearly_anim.duration(),
        )

    values = []
    finished = []
    view._yearly_anim.valueChanged.connect(lambda _value: values.append(snapshot()))
    view._yearly_anim.finished.connect(lambda: finished.append(snapshot()))
    view.clear()

    assert view._stack.currentIndex() == view._PAGE_YEARLY
    assert view._yearly_widget._countdown_remaining == 55
    assert view._yearly_timer_exit_pending is True
    assert view._yearly_anim.duration() == view._YEARLY_TIMER_EXIT_FADE_DURATION_MS
    assert view._yearly_widget.graphicsEffect() is view._yearly_opacity

    # Exercise the real Qt animation and its finished handlers using its public
    # clock. Starting the unified animation timer is queued; wall-clock time
    # spent exposing/painting a window cannot measure these two logical phases.
    exit_ms = view._YEARLY_TIMER_EXIT_FADE_DURATION_MS
    idle_ms = view._YEARLY_FADE_IN_DURATION_MS
    assert view._yearly_anim.state() is QAbstractAnimation.State.Running
    view._yearly_anim.setCurrentTime(exit_ms // 2)
    assert snapshot() == (True, 55, view._PAGE_YEARLY, 0.5, exit_ms)
    assert finished == []
    view._yearly_anim.setCurrentTime(exit_ms)
    assert finished == [(False, None, view._PAGE_YEARLY, 0.0, idle_ms)]
    assert view._yearly_anim.state() is QAbstractAnimation.State.Running
    view._yearly_anim.setCurrentTime(idle_ms // 2)
    assert snapshot() == (False, None, view._PAGE_YEARLY, 0.5, idle_ms)
    assert len(finished) == 1
    view._yearly_anim.setCurrentTime(idle_ms)
    assert len(finished) == 2
    fade_out = [state for state in values if state[0]]
    assert any(0.0 < state[3] < 1.0 for state in fade_out)
    assert all(state[1] == 55 and state[2] == view._PAGE_YEARLY for state in fade_out)
    # The production finished handler clears the countdown and starts idle fade-in.
    assert finished[0] == (False, None, view._PAGE_YEARLY, 0.0, view._YEARLY_FADE_IN_DURATION_MS)
    fade_in = [state for state in values if not state[0]]
    assert any(0.0 < state[3] < 1.0 for state in fade_in)
    assert all(state[1] is None and state[2] == view._PAGE_YEARLY for state in fade_in)
    assert view._yearly_timer_exit_pending is False
    assert view._yearly_widget._countdown_remaining is None
    assert view._stack.currentIndex() == view._PAGE_YEARLY
    assert view._yearly_anim.duration() == view._YEARLY_FADE_IN_DURATION_MS
    assert view._yearly_opacity.opacity() == 1.0
    assert view._yearly_anim.state() is QAbstractAnimation.State.Stopped


def test_immediate_countdown_clear_always_requests_a_repaint():
    widget = _UpdateTrackingYearlyTextWidget()
    widget.set_countdown(55, 90)
    widget.update_requests = 0

    widget.clear_countdown()

    assert widget._countdown_remaining is None
    assert widget.update_requests >= 1


def test_new_projection_cancels_pending_yearly_page_fade_out(request):
    view = _ProjectionViewHarness()
    request.addfinalizer(lambda: dispose_widget(view))
    view.resize(1280, 720)
    view.set_yearly_text("Annual text", "Reference")
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
    # Arming video preserves the current page until a usable frame arrives.
    assert view._stack.currentIndex() == view._PAGE_YEARLY
    assert view._yearly_anim.state() is QAbstractAnimation.State.Stopped


def test_new_untransformed_image_resets_zoom_before_clear_fade_finishes():
    view = _ProjectionViewHarness()
    image = QImage(160, 90, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    view.show_image_from_qimage(image)
    view.set_image_transform(2.0, 0.2, -0.1, animate=False)

    view.clear()
    view.show_image_from_qimage(image, cache_pixmap=False)

    assert view.display_label._image_transform.current == IDENTITY_IMAGE_TRANSFORM


def test_fallback_media_fades_in_and_out_through_the_qt_renderer() -> None:
    view = _ProjectionViewHarness()
    try:
        image = QImage(160, 90, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        fade_finished = QSignalSpy(view._media_anim.finished)

        view.show_image_from_qimage(image)

        assert view._stack.currentIndex() == view._PAGE_MEDIA
        assert view.display_label.graphicsEffect() is view._media_opacity
        assert not hasattr(view.display_label, "_fade_timer")
        assert view._media_anim.duration() == view._MEDIA_FADE_DURATION_MS
        assert view._media_anim.state() is QAbstractAnimation.State.Running
        assert fade_finished.wait(view._MEDIA_FADE_DURATION_MS + 1_000)
        assert view._media_opacity.opacity() == 1.0

        fade_finished = QSignalSpy(view._media_anim.finished)
        view.clear()

        assert not view._is_showing_media
        assert view._stack.currentIndex() == view._PAGE_MEDIA
        assert view._media_anim.state() is QAbstractAnimation.State.Running
        assert fade_finished.wait(view._MEDIA_FADE_DURATION_MS + 1_000)
        assert view._stack.currentIndex() == view._PAGE_YEARLY
    finally:
        view.deleteLater()


def test_native_media_output_never_runs_the_qt_media_fade() -> None:
    view = _ProjectionViewHarness()
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))

    view.set_native_output_active(True)
    view.show_image_from_qimage(image)

    assert view.native_output_active
    assert view.native_video_surface.graphicsEffect() is None
    assert view._media_anim.state() is QAbstractAnimation.State.Stopped
    assert view._media_opacity.opacity() == 1.0

    view.clear()

    assert view._media_anim.state() is QAbstractAnimation.State.Stopped
    assert view.native_output_active
    assert not view.native_video_surface.isHidden()
    assert view._stack.currentIndex() == view._PAGE_MEDIA



def test_static_yearly_fallback_stays_visible_on_repeated_idle_clear(request):
    view = _ProjectionViewHarness()
    request.addfinalizer(lambda: dispose_widget(view))
    view.set_yearly_text("Annual text", "Reference")
    finished = QSignalSpy(view._yearly_anim.finished)

    view.clear()
    view.clear()

    assert view._stack.count() == 3
    assert view._stack.currentWidget() is view._yearly_widget
    assert view._yearly_opacity.opacity() == 1.0
    assert view._yearly_anim.state() is QAbstractAnimation.State.Stopped
    assert finished.count() == 0


def test_circular_timer_clear_fades_back_to_static_yearly_text(request):
    view = _ProjectionViewHarness()
    request.addfinalizer(lambda: dispose_widget(view))
    view.set_yearly_text("Annual text", "Reference")
    view.show_timer(30, 60, MediaCountdownPresentation.CIRCULAR)
    view._timer_anim.setCurrentTime(view._timer_anim.duration())
    assert view._stack.currentWidget() is view._proj_timer

    view.clear()

    assert view._timer_presentation is None
    assert view._yearly_widget._countdown_remaining is None
    assert view._stack.currentWidget() is view._yearly_widget
    assert view._timer_anim.state() is QAbstractAnimation.State.Stopped
    assert view._yearly_anim.state() is QAbstractAnimation.State.Running
    view._yearly_anim.setCurrentTime(view._YEARLY_FADE_IN_DURATION_MS // 2)
    assert view._yearly_opacity.opacity() == 0.5
    view._yearly_anim.setCurrentTime(view._YEARLY_FADE_IN_DURATION_MS)
    assert view._yearly_opacity.opacity() == 1.0



def test_native_renderer_unavailable_restores_static_yearly_fallback(request):
    view = _ProjectionViewHarness()
    request.addfinalizer(lambda: dispose_widget(view))
    view.set_yearly_text("Annual text", "Reference")
    view.set_native_output_active(True)
    view.clear()
    assert view._stack.currentWidget() is view._media_host

    view.set_native_output_active(False)
    view.clear()

    assert view.native_video_surface.isHidden()
    assert view._stack.currentWidget() is view._yearly_widget
    assert view._yearly_widget._countdown_remaining is None
    assert view._yearly_opacity.opacity() == 1.0
    assert view._yearly_anim.state() is QAbstractAnimation.State.Stopped
    assert view._media_anim.state() is QAbstractAnimation.State.Stopped
