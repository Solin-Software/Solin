from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from solin.controllers.program_content_controller import ProgramContentController
from solin.core.projection.application import ProjectionSession
from solin.core.timer.models import MediaCountdownPresentation


_APP = QApplication.instance() or QApplication([])


class _FontManager(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        pass

    def family(self, _name: str) -> str:
        return "Arial"


def test_program_content_publishes_idle_and_timer_surfaces() -> None:
    session = ProjectionSession()
    frames: list[QImage] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda frame: frames.append(QImage(frame)),
        lambda: ("A yearly quotation", "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=320,
        height=180,
    )

    controller.refresh()
    assert frames[-1].size().width() == 320
    assert frames[-1].size().height() == 180

    session.set_state(
        {
            "type": "timer",
            "remaining": 42,
            "total": 60,
            "presentation": MediaCountdownPresentation.CIRCULAR.value,
        }
    )
    assert len(frames) >= 2
    controller.close()


def test_program_content_accepts_custom_idle_frames_only_while_idle() -> None:
    session = ProjectionSession()
    frames: list[object] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None,
        width=320,
        height=180,
    )
    custom_idle = QImage(16, 9, QImage.Format.Format_ARGB32)

    controller.submit_idle_frame(custom_idle)
    assert custom_idle not in frames

    session.set_idle_media_path("configured-idle.mp4")
    controller.submit_idle_frame(custom_idle)
    assert frames[-1] is custom_idle

    session.set_state({"type": "image"})
    before = len(frames)
    controller.submit_idle_frame(custom_idle)
    assert len(frames) == before
    controller.close()


def test_program_content_unsubscribes_on_close() -> None:
    session = ProjectionSession()
    frames: list[object] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None,
        width=320,
        height=180,
    )
    controller.refresh()
    controller.close()
    before = len(frames)

    session.reset_state()

    assert len(frames) == before


def test_program_content_requests_current_identity_before_publishing() -> None:
    session = ProjectionSession()
    events: list[tuple[str, int]] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: events.append(("frame", session.session_id)),
        lambda: ("", "", ""),
        media_epoch_sink=lambda epoch: events.append(("epoch", epoch)),
        width=320,
        height=180,
    )

    session.set_state(
        {
            "type": "timer",
            "remaining": 10,
            "total": 20,
            "presentation": MediaCountdownPresentation.CIRCULAR.value,
        }
    )

    assert events[-2:] == [("epoch", 1), ("frame", 1)]
    controller.close()
