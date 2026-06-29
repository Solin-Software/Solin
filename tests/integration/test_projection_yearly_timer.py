from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from solin.projection.window import YearlyTextWidget


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
    safe_bottom = int(countdown_layout[2].top()) - max(
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
    countdown_top = int(countdown_layout[2].top())
    safe_bottom = countdown_top - max(8, int(widget.height() * 0.025))

    for y in range(safe_bottom, countdown_top):
        for x in range(int(widget.width() * 0.2), int(widget.width() * 0.8)):
            assert image.pixelColor(x, y).lightness() <= 8
