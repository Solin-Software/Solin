from __future__ import annotations

from solin.core.timer.models import MediaCountdownPresentation
from solin.widgets.projection.bar import ProjectionBar


class _CircularPreview:
    def __init__(self) -> None:
        self.updates = []
        self.blinks = []

    def update_data(self, remaining: int, total: int) -> None:
        self.updates.append((remaining, total))

    def set_blink(self, on: bool) -> None:
        self.blinks.append(on)


class _YearlyPreview:
    def __init__(self) -> None:
        self.updates = []
        self.blinks = []

    def set_countdown(self, remaining: int, total: int) -> None:
        self.updates.append((remaining, total))

    def set_countdown_blink(self, on: bool) -> None:
        self.blinks.append(on)


class _Stack:
    def __init__(self) -> None:
        self.current = None

    def setCurrentWidget(self, widget) -> None:
        self.current = widget


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


def _timer_bar(presentation: MediaCountdownPresentation) -> ProjectionBar:
    bar = ProjectionBar.__new__(ProjectionBar)
    bar._timer_presentation = presentation
    bar._timer_total_secs = 90
    bar.circular_timer = _CircularPreview()
    bar.yearly_timer = _YearlyPreview()
    bar.overlay_stack = _Stack()
    bar.timer_countdown_label = _Label()
    return bar


def test_expanded_timer_preview_matches_yearly_text_presentation():
    bar = _timer_bar(MediaCountdownPresentation.YEARLY_TEXT)

    bar._update_timer_preview(55)
    bar._set_timer_preview_blink(True)

    assert bar.yearly_timer.updates == [(55, 90)]
    assert bar.yearly_timer.blinks == [True]
    assert bar.overlay_stack.current is bar.yearly_timer
    assert bar.circular_timer.updates == []
    assert bar.circular_timer.blinks == []


def test_expanded_timer_preview_keeps_existing_circular_presentation():
    bar = _timer_bar(MediaCountdownPresentation.CIRCULAR)

    bar._update_timer_preview(55)
    bar._set_timer_preview_blink(True)

    assert bar.circular_timer.updates == [(55, 90)]
    assert bar.circular_timer.blinks == [True]
    assert bar.overlay_stack.current is bar.circular_timer
    assert bar.yearly_timer.updates == []
    assert bar.yearly_timer.blinks == []


def test_bar_label_only_keeps_hour_field_for_yearly_text_presentation():
    yearly_bar = _timer_bar(MediaCountdownPresentation.YEARLY_TEXT)
    yearly_bar._timer_total_secs = 90 * 60
    circular_bar = _timer_bar(MediaCountdownPresentation.CIRCULAR)
    circular_bar._timer_total_secs = 90 * 60

    yearly_bar._update_timer_bar_label(59 * 60 + 59)
    circular_bar._update_timer_bar_label(59 * 60 + 59)

    assert yearly_bar.timer_countdown_label.text == "00:59:59"
    assert circular_bar.timer_countdown_label.text == "59:59"
