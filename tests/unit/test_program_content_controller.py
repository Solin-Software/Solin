from __future__ import annotations

import os
import subprocess
import sys

import pytest
import shiboken6

from solin.controllers import program_content_controller as program_module

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from solin.controllers.program_content_controller import ProgramContentController
from solin.core.projection.application import ProjectionSession
from solin.core.projection.image_framing import ImageTransform
from solin.core.timer.models import MediaCountdownPresentation


_APP = QApplication.instance() or QApplication([])


_PARENTLESS_GC_DIAGNOSTIC = r'''
import gc
import json
import threading
import weakref
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication
from solin.controllers.program_content_controller import ProgramContentController
from solin.core.projection.application import ProjectionSession

app = QApplication([])
destroyed, finalized = [], []

class Fonts(QObject):
    font_ready = Signal(str)
    def ensure(self, _name): pass
    def family(self, _name): return "Arial"

def prepare():
    controller = ProgramContentController(
        ProjectionSession(), Fonts(), lambda _frame: None, lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None, width=64, height=36, parent=None,
    )
    refs = []
    for label, obj in (("yearly", controller._yearly_widget),
                       ("timer", controller._timer_widget), ("controller", controller)):
        obj.destroyed.connect(
            lambda _obj=None, label=label: destroyed.append((label, QThread.isMainThread())),
            Qt.ConnectionType.DirectConnection,
        )
        # Neither observation keeps the QObject wrapper alive.
        refs.append(weakref.ref(obj))
        weakref.finalize(obj, lambda label=label: finalized.append((label, QThread.isMainThread())))
    try:
        app.processEvents()
    finally:
        controller.close()
    return refs

refs = prepare()
worker = threading.Thread(target=gc.collect)
worker.start()
worker.join(timeout=5)
assert not worker.is_alive(), "worker GC did not finish"
print(json.dumps({"phase": "worker_gc", "destroyed": destroyed,
                  "finalized": finalized, "alive": [ref() is not None for ref in refs]}), flush=True)
assert not destroyed, "native GUI objects were destroyed before GUI deferred destruction"
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
print(json.dumps({"phase": "gui_drain", "destroyed": destroyed,
                  "finalized": finalized, "alive": [ref() is not None for ref in refs]}), flush=True)
assert destroyed == [("yearly", True), ("timer", True), ("controller", True)], destroyed
assert all(ref() is None for ref in refs), "closed wrappers remained retained"
'''


class _FontManager(QObject):
    font_ready = Signal(str)

    def ensure(self, _name: str) -> None:
        pass

    def family(self, _name: str) -> str:
        return "Arial"


@pytest.fixture
def content_factory(request, qt_object_owner):
    """Register shutdown as soon as construction succeeds, before any assertion."""
    def create(*args, **kwargs):
        kwargs.setdefault("parent", qt_object_owner)
        controller = ProgramContentController(*args, **kwargs)
        request.addfinalizer(controller.close)
        return controller

    return create


def _idle_controller(content_factory, session=None, **kwargs):
    return content_factory(
        session if session is not None else ProjectionSession(),
        _FontManager(), lambda _frame: None, lambda: ("", "", ""),
        media_epoch_sink=lambda _epoch: None, width=64, height=36, **kwargs,
    )


def test_close_consumes_subscription_once_and_destroys_native_objects_on_gui(content_factory):
    session = ProjectionSession()
    controller = _idle_controller(content_factory, session)
    objects = (controller._yearly_widget, controller._timer_widget, controller)
    destroyed = []
    for index, obj in enumerate(objects):
        obj.destroyed.connect(
            lambda _obj=None, index=index: destroyed.append((index, QThread.isMainThread())),
            Qt.ConnectionType.DirectConnection,
        )

    controller.close()
    controller.close()

    assert controller._unsubscribe is None
    assert not session._listeners
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == [(0, True), (1, True), (2, True)]
    assert all(not shiboken6.isValid(obj) for obj in objects)
    controller.close()
    controller.submit_frame(object())
    controller.refresh()
    controller.update_yearly_text("Quote", "Reference", "E")
    controller.set_timer_blink(True)
    controller._on_projection_changed()
    controller._initialize_content()


def test_close_cancels_deferred_startup_before_callbacks_run(content_factory, monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        ProgramContentController, "render_yeartext_source_image",
        lambda _self: callbacks.append("yeartext"),
    )
    monkeypatch.setattr(
        ProgramContentController, "refresh", lambda _self: callbacks.append("refresh"),
    )
    controller = _idle_controller(content_factory)
    assert callbacks == []

    controller.close()
    _APP.processEvents()

    assert callbacks == []


def test_close_schedules_destruction_even_when_unsubscribe_raises(content_factory, monkeypatch):
    session = ProjectionSession()
    controller = _idle_controller(content_factory, session)
    unsubscribe = controller._unsubscribe
    objects = (controller._yearly_widget, controller._timer_widget, controller)

    def fail_unsubscribe():
        unsubscribe()
        raise RuntimeError("unsubscribe failed")

    monkeypatch.setattr(controller, "_unsubscribe", fail_unsubscribe)
    with pytest.raises(RuntimeError, match="unsubscribe failed"):
        controller.close()

    controller.close()
    assert controller._unsubscribe is None
    assert not session._listeners
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(not shiboken6.isValid(obj) for obj in objects)


def test_closed_parentless_content_is_destroyed_on_gui_after_worker_gc(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-c", _PARENTLESS_GC_DIAGNOSTIC],
        cwd=tmp_path, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failing_widget", ["_yearly_widget", "_timer_widget"])
def test_close_continues_native_cleanup_when_widget_close_raises(
    content_factory, monkeypatch, request, failing_widget,
):
    controller = _idle_controller(content_factory)
    objects = (controller._yearly_widget, controller._timer_widget, controller)
    for obj in objects:
        # Keep the failing implementation's orphaned widgets safe after assertion failure.
        request.addfinalizer(lambda obj=obj: obj.deleteLater() if shiboken6.isValid(obj) else None)

    def fail_close():
        raise RuntimeError("widget close failed")

    monkeypatch.setattr(getattr(controller, failing_widget), "close", fail_close)
    with pytest.raises(RuntimeError, match="widget close failed"):
        controller.close()

    controller.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(not shiboken6.isValid(obj) for obj in objects)


def test_program_content_publishes_idle_and_timer_surfaces(content_factory) -> None:
    session = ProjectionSession()
    frames: list[QImage] = []
    controller = content_factory(
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


def test_program_content_unsubscribes_on_close(content_factory) -> None:
    session = ProjectionSession()
    frames: list[object] = []
    controller = content_factory(
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


def test_program_content_requests_current_identity_before_publishing(content_factory) -> None:
    session = ProjectionSession()
    events: list[tuple[str, int]] = []
    controller = content_factory(
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


def test_program_content_uses_canonical_output_canvas_for_image_framing(content_factory) -> None:
    session = ProjectionSession()
    events: list[tuple[object, ...]] = []
    controller = content_factory(
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


def test_program_content_retargets_the_active_image_without_relabeling_media(content_factory) -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    controller = content_factory(
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


def test_program_content_keeps_retained_image_framing_stable_on_refresh(content_factory) -> None:
    session = ProjectionSession()
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    controller = content_factory(
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


def test_program_content_does_not_republish_static_video_controls_per_frame(content_factory) -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    transforms: list[tuple[ImageTransform | None, dict[str, object]]] = []
    frames: list[object] = []
    controller = content_factory(
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


def test_program_content_keeps_the_idle_epoch_during_audio_only_playback(content_factory) -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    controller = content_factory(
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


def test_program_content_publishes_idle_once_when_audio_replaces_visual_media(content_factory) -> None:
    session = ProjectionSession()
    epochs: list[int] = []
    controller = content_factory(
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


def test_idle_publishes_a_transparent_frame_not_the_year_text(content_factory) -> None:
    """Idle is not content: the year text belongs to its own scene source."""
    session = ProjectionSession()
    frames: list[QImage] = []
    controller = content_factory(
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


def test_idle_blanks_the_content_channel_only_once_per_presentation(content_factory) -> None:
    session = ProjectionSession()
    frames: list[object] = []
    controller = content_factory(
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
def test_idle_content_is_transparent_regardless_of_the_shared_source(idle_path, audio_only, content_factory):
    session = ProjectionSession()
    frames = []
    controller = content_factory(
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


def test_idle_change_during_active_image_preserves_content_and_epoch(content_factory):
    session = ProjectionSession()
    frames, epochs = [], []
    controller = content_factory(
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


def test_yeartext_renderer_announces_only_committed_explicit_path(tmp_path, monkeypatch, content_factory):
    path = tmp_path / "yeartext.png"
    unused = tmp_path / "unused.png"
    monkeypatch.setenv("SOLIN_YEARTEXT_IMAGE", str(unused))
    events = []
    controller = content_factory(
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
def test_failed_yeartext_write_retains_file_and_revision(tmp_path, monkeypatch, failure, content_factory):
    path = tmp_path / "yeartext.png"
    original = b"previous committed image"
    path.write_bytes(original)
    events = []
    controller = content_factory(
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


def test_yeartext_update_preserves_active_yearly_countdown(tmp_path, content_factory):
    session = ProjectionSession()
    frames = []
    quote = ["Original"]
    controller = content_factory(
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


def test_initial_yeartext_render_waits_for_settings_and_publishes_revision(tmp_path, content_factory):
    rendered = []
    settings_ready = False

    def yearly_text():
        assert settings_ready, "startup must not read settings during construction"
        return "Quote", "Reference", "E"

    path = tmp_path / "yeartext.png"
    controller = content_factory(
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
        _APP.processEvents()
        assert rendered == [(str(path), 1)]
        assert not QImage(str(path)).isNull()
    finally:
        controller.close()
