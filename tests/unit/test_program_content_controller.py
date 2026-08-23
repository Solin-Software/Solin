from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from solin.controllers.program_content_controller import ProgramContentController
from solin.core.projection.application import ProjectionSession
from solin.core.projection.aspect_ratio import ProjectionAspectRatio
from solin.core.projection.image_framing import ImageTransform
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


def test_program_content_publishes_image_framing_before_the_bound_frame() -> None:
    session = ProjectionSession()
    events: list[tuple[object, ...]] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: events.append(("frame", session.session_id)),
        lambda: ("", "", ""),
        media_epoch_sink=lambda epoch: events.append(("epoch", epoch)),
        image_transform_sink=lambda transform, **options: events.append(
            ("transform", transform, options)
        ),
        projection_aspect_ratio_provider=lambda: ProjectionAspectRatio(4, 3),
        width=320,
        height=180,
    )
    image_data = b"active-image"
    session.set_state(
        {
            "type": "image",
            "data": image_data,
            "transform": (1.5, 0.1, -0.2),
        }
    )

    controller.submit_frame(QImage(16, 9, QImage.Format.Format_ARGB32))

    assert events[-3:] == [
        ("epoch", 1),
        (
            "transform",
            ImageTransform(1.5, 0.1, -0.2),
            {
                "media_epoch": 1,
                "canvas_width": 240,
                "canvas_height": 180,
                "animate": False,
            },
        ),
        ("frame", 1),
    ]
    controller.close()


def test_program_content_retargets_the_active_image_without_relabeling_media() -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: None,
        lambda: ("", "", ""),
        media_epoch_sink=epochs.append,
        image_transform_sink=lambda transform, **options: transforms.append(
            (transform, options)
        ),
        projection_aspect_ratio_provider=lambda: ProjectionAspectRatio(16, 9),
        width=320,
        height=180,
    )
    image_data = b"active-image"
    session.set_state(
        {
            "type": "image",
            "data": image_data,
            "transform": (1.0, 0.0, 0.0),
        }
    )
    controller.submit_frame(QImage(16, 9, QImage.Format.Format_ARGB32))
    transforms.clear()
    epochs.clear()

    session.update_image_transform((2.0, 0.25, -0.1), animate=True)

    assert epochs == []
    assert transforms == [
        (
            ImageTransform(2.0, 0.25, -0.1),
            {
                "media_epoch": 1,
                "canvas_width": 320,
                "canvas_height": 180,
                "animate": True,
            },
        )
    ]
    controller.close()


def test_program_content_reframes_a_retained_image_when_target_aspect_changes() -> None:
    session = ProjectionSession()
    ratios = [ProjectionAspectRatio(16, 9)]
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: None,
        lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None,
        image_transform_sink=lambda transform, **options: transforms.append(
            (transform, options)
        ),
        projection_aspect_ratio_provider=lambda: ratios[-1],
        width=320,
        height=180,
    )
    session.set_state(
        {
            "type": "image",
            "data": b"active-image",
            "transform": (1.5, 0.0, 0.0),
        }
    )
    controller.submit_frame(QImage(16, 9, QImage.Format.Format_ARGB32))
    transforms.clear()

    ratios.append(ProjectionAspectRatio(4, 3))
    controller.refresh()

    assert transforms == [
        (
            ImageTransform(1.5, 0.0, 0.0),
            {
                "media_epoch": 1,
                "canvas_width": 240,
                "canvas_height": 180,
                "animate": False,
            },
        )
    ]
    controller.close()
