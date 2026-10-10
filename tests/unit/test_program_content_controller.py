from __future__ import annotations

import pytest

from solin.controllers import program_content_controller as program_module

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from solin.controllers.program_content_controller import ProgramContentController
from solin.core.projection.application import ProjectionSession
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


def test_program_content_uses_canonical_output_canvas_for_image_framing() -> None:
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
        width=320,
        height=240,
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
                "canvas_width": 320,
                "canvas_height": 240,
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
        image_transform_sink=lambda transform, **options: transforms.append((transform, options)),
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


def test_program_content_keeps_retained_image_framing_stable_on_refresh() -> None:
    session = ProjectionSession()
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: None,
        lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None,
        image_transform_sink=lambda transform, **options: transforms.append((transform, options)),
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

    controller.refresh()

    assert transforms == []
    controller.close()


def test_program_content_does_not_republish_static_video_controls_per_frame() -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    frames: list[object] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("", "", ""),
        media_epoch_sink=epochs.append,
        image_transform_sink=lambda transform, **options: transforms.append((transform, options)),
        width=320,
        height=180,
    )
    session.set_state({"type": "video"})
    first = object()
    second = object()

    controller.submit_frame(first)
    controller.submit_frame(second)

    assert epochs == [1]
    assert transforms == [
        (
            None,
            {
                "media_epoch": 1,
                "canvas_width": 320,
                "canvas_height": 180,
                "animate": False,
            },
        )
    ]
    assert frames == [first, second]
    controller.close()


def test_program_content_keeps_the_idle_epoch_during_audio_only_playback() -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: None,
        lambda: ("", "", ""),
        media_epoch_sink=epochs.append,
        width=320,
        height=180,
    )
    controller.refresh()
    epochs.clear()

    session.set_state({"type": "video", "is_audio": True, "title": "Song"})
    session.reset_state()

    assert epochs == []
    controller.close()


def test_program_content_publishes_idle_once_when_audio_replaces_visual_media() -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda _frame: None,
        lambda: ("", "", ""),
        media_epoch_sink=epochs.append,
        width=320,
        height=180,
    )
    session.set_state({"type": "image", "data": b"image"})
    controller.submit_frame(QImage(16, 9, QImage.Format.Format_ARGB32))
    epochs.clear()

    session.set_state({"type": "video", "is_audio": True, "title": "Song"})
    audio_idle_epoch = session.presentation_session_id
    session.reset_state()

    assert epochs == [audio_idle_epoch]
    controller.close()


def test_idle_publishes_a_transparent_frame_not_the_year_text() -> None:
    """Idle is not content: the year text belongs to its own scene source."""
    session = ProjectionSession()
    frames: list[QImage] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        lambda frame: frames.append(QImage(frame)),
        lambda: ("A yearly quotation", "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
    )

    controller.refresh()

    published = frames[-1]
    assert published.size().width() == 64
    assert all(
        published.pixelColor(x, y).alpha() == 0 for x in range(0, 64, 8) for y in range(0, 36, 6)
    ), "the content source must composite away entirely while idle"
    controller.close()


def test_idle_blanks_the_content_channel_only_once_per_presentation() -> None:
    session = ProjectionSession()
    frames: list[object] = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
    )

    controller.refresh()
    controller.refresh()
    blanks_after_idle = len(frames)
    controller.refresh()
    assert len(frames) == blanks_after_idle

    # Media takes the channel back, and the next idle blanks it again.
    session.set_state({"type": "video"})
    controller.submit_frame(object())
    session.reset_state()
    assert len(frames) > blanks_after_idle + 1
    controller.close()


@pytest.mark.parametrize("idle_path", ["", "idle.png", "idle.mp4"])
@pytest.mark.parametrize("audio_only", [False, True])
def test_idle_content_is_transparent_regardless_of_the_shared_source(idle_path, audio_only):
    session = ProjectionSession()
    frames = []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("Quote", "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
    )
    try:
        session.set_state({"type": "image", "data": b"image"})
        controller.submit_frame(object())
        session.set_idle_media_path(idle_path)
        if audio_only:
            session.set_state({"type": "video", "is_audio": True})
        else:
            session.reset_state()
        assert isinstance(frames[-1], QImage)
        assert frames[-1].pixelColor(32, 18).alpha() == 0
        count = len(frames)
        session.set_idle_media_path("replacement.mp4")
        controller.refresh()
        assert len(frames) == count
    finally:
        controller.close()


def test_idle_change_during_active_image_preserves_content_and_epoch():
    session = ProjectionSession()
    frames, epochs = [], []
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: ("", "", ""),
        media_epoch_sink=epochs.append,
        width=64,
        height=36,
    )
    try:
        session.set_state({"type": "image", "data": b"image"})
        frame = object()
        controller.submit_frame(frame)
        count, epoch = len(frames), session.presentation_session_id
        session.set_idle_media_path("idle.mp4")
        assert frames == [frame]
        assert len(frames) == count
        assert epochs == [epoch]
    finally:
        controller.close()


def test_yeartext_renderer_announces_only_committed_explicit_path(tmp_path, monkeypatch):
    path = tmp_path / "yeartext.png"
    unused = tmp_path / "unused.png"
    monkeypatch.setenv("SOLIN_YEARTEXT_IMAGE", str(unused))
    events = []
    controller = ProgramContentController(
        ProjectionSession(),
        _FontManager(),
        lambda _frame: None,
        lambda: ("Quote", "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
        yeartext_image_path=str(path),
        yeartext_reloaded=lambda actual_path, revision: events.append(
            (actual_path, revision, QImage(actual_path).isNull())
        ),
    )
    try:
        controller.render_yeartext_source_image()
        controller.update_yearly_text("Quote", "Reference", "E")
        assert events == [(str(path), 1, False), (str(path), 2, False)]
        assert not unused.exists()
    finally:
        controller.close()
    controller.render_yeartext_source_image()
    assert len(events) == 2


@pytest.mark.parametrize("failure", ["open", "encode", "commit"])
def test_failed_yeartext_write_retains_file_and_revision(tmp_path, monkeypatch, failure):
    path = tmp_path / "yeartext.png"
    original = b"previous committed image"
    path.write_bytes(original)
    events = []
    controller = ProgramContentController(
        ProjectionSession(),
        _FontManager(),
        lambda _frame: None,
        lambda: ("Quote", "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
        yeartext_image_path=str(path),
        yeartext_reloaded=lambda *args: events.append(args),
    )
    try:
        with monkeypatch.context() as patch:
            if failure == "encode":
                patch.setattr(program_module.QImageWriter, "write", lambda *_args: False)
            elif failure == "open":
                patch.setattr(program_module.QSaveFile, "open", lambda *_args: False)
            else:

                def fail_commit(output):
                    output.cancelWriting()
                    return False

                patch.setattr(program_module.QSaveFile, "commit", fail_commit)
            controller.render_yeartext_source_image()
            assert path.read_bytes() == original
            assert events == []
        controller.render_yeartext_source_image()
        assert events == [(str(path), 1)]
    finally:
        controller.close()


def test_yeartext_update_preserves_active_yearly_countdown(tmp_path):
    session = ProjectionSession()
    frames = []
    quote = ["Original"]
    controller = ProgramContentController(
        session,
        _FontManager(),
        frames.append,
        lambda: (quote[0], "Reference", "E"),
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
        yeartext_image_path=str(tmp_path / "yeartext.png"),
    )
    try:
        session.set_state(
            {
                "type": "timer",
                "remaining": 30,
                "total": 60,
                "presentation": MediaCountdownPresentation.YEARLY_TEXT.value,
            }
        )
        before = len(frames)
        quote[0] = "Updated"
        controller.update_yearly_text("Updated", "Reference", "E")
        assert len(frames) == before + 1
        assert session.state["remaining"] == 30
        assert session.state["presentation"] == MediaCountdownPresentation.YEARLY_TEXT.value
    finally:
        controller.close()


def test_initial_yeartext_render_waits_for_settings_and_publishes_revision(tmp_path, monkeypatch):
    pending, rendered = [], []
    monkeypatch.setattr(
        program_module.QTimer, "singleShot", lambda _ms, callback: pending.append(callback)
    )
    settings_ready = False

    def yearly_text():
        assert settings_ready, "startup must not read settings during construction"
        return "Quote", "Reference", "E"

    path = tmp_path / "yeartext.png"
    controller = ProgramContentController(
        ProjectionSession(),
        _FontManager(),
        lambda _frame: None,
        yearly_text,
        media_epoch_sink=lambda _epoch: None,
        width=64,
        height=36,
        yeartext_image_path=str(path),
        yeartext_reloaded=lambda *args: rendered.append(args),
    )
    try:
        assert not path.exists()
        assert rendered == []
        settings_ready = True
        for callback in pending:
            callback()
        assert rendered == [(str(path), 1)]
        assert not QImage(str(path)).isNull()
    finally:
        controller.close()
