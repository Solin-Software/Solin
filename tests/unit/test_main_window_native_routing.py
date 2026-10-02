from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtGui import QImage

import solin.main_window as main_window
from solin.core.scenes.frame_channel import VideoFrame
from solin.core.scenes.model import BusId, VideoPixelFormat
from solin.core.scenes.recording import ProgramRecordingState, ProgramRecordingStatus
from solin.main_window import (
    MainWindow,
    _native_scene_routing_supported,
    _use_native_media_presentation,
)


def test_native_routing_supported_on_windows_native_engine(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", True)
    assert _native_scene_routing_supported()


def test_native_routing_supported_under_libobs_on_xcb(monkeypatch) -> None:
    # The libobs sidecar paints projection windows by binding an obs_display to a
    # shared native handle; X11/XWayland (xcb) exposes one, so routing is enabled.
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", False)
    monkeypatch.setattr(main_window, "libobs_scene_engine_selected", lambda: True)
    monkeypatch.setattr(
        "PySide6.QtGui.QGuiApplication.platformName", staticmethod(lambda: "xcb")
    )
    assert _native_scene_routing_supported()


def test_native_routing_unsupported_under_libobs_on_wayland(monkeypatch) -> None:
    # Native Wayland cannot share a top-level window handle cross-process, so the
    # sidecar cannot paint into it — those sessions keep the CPU-readback path.
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", False)
    monkeypatch.setattr(main_window, "libobs_scene_engine_selected", lambda: True)
    monkeypatch.setattr(
        "PySide6.QtGui.QGuiApplication.platformName", staticmethod(lambda: "wayland")
    )
    assert not _native_scene_routing_supported()


def test_native_routing_unsupported_without_engine_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", False)
    monkeypatch.setattr(main_window, "libobs_scene_engine_selected", lambda: False)
    assert not _native_scene_routing_supported()


def test_every_media_surface_uses_native_presentation_for_raw_visual_or_program() -> None:
    assert _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=False,
        raw_visual=True,
    )
    assert _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=True,
        raw_visual=False,
    )
    assert not _use_native_media_presentation(
        native_window_routing_ready=True,
        mirror_enabled=False,
        raw_visual=False,
    )
    assert not _use_native_media_presentation(
        native_window_routing_ready=False,
        mirror_enabled=True,
        raw_visual=True,
    )


class _ProjectionBar:
    def __init__(self, *, requested: bool) -> None:
        self.native_video_output_requested = requested
        self.native_video_output_surface = object()
        self.active_states: list[bool] = []

    def set_native_video_output_active(self, active: bool) -> None:
        self.active_states.append(active)


class _SceneRuntime:
    native_window_routing_ready = True

    def __init__(self) -> None:
        self.targets = ()

    def set_window_targets(self, targets) -> None:
        self.targets = targets


def _native_preview_host(*, requested: bool):
    projection_bar = _ProjectionBar(requested=requested)
    runtime = _SceneRuntime()
    host = SimpleNamespace(
        _program_mirror_enabled=lambda: False,
        _native_window_output_suppressed=False,
        projection_session=SimpleNamespace(
            state={"type": "video", "is_audio": False},
            projection_windows=(),
            all_windows=lambda: (),
        ),
        proj_bar=projection_bar,
        scene_runtime=runtime,
        _native_window_target=(
            lambda surface, target_id, bus_id: (surface, target_id, bus_id)
        ),
        _native_fallback_mirror_required=False,
        _reconcile_scene_media_egress=lambda: None,
        _projection_targets=SimpleNamespace(
            restore_state_to_window=lambda _window: None,
        ),
    )
    return host, projection_bar, runtime


def test_operator_video_output_uses_the_raw_native_bus(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", True)
    host, projection_bar, runtime = _native_preview_host(requested=True)

    MainWindow._reconcile_native_scene_surfaces(cast(MainWindow, host))

    assert projection_bar.active_states == [True]
    assert runtime.targets == (
        (
            projection_bar.native_video_output_surface,
            "media-control-video",
            BusId.MEDIA_WINDOWS,
        ),
    )


def test_operator_video_output_keeps_qt_fallback_without_native_routing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", False)
    host, projection_bar, runtime = _native_preview_host(requested=True)

    MainWindow._reconcile_native_scene_surfaces(cast(MainWindow, host))

    assert projection_bar.active_states == [False]
    assert runtime.targets == ()


def test_losing_native_routing_restores_the_existing_window_in_place(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", True)
    host, _projection_bar, runtime = _native_preview_host(requested=False)
    runtime.native_window_routing_ready = False
    restored: list[object] = []

    class Window:
        native_output_active = True
        native_video_surface = object()

        def set_native_output_active(self, active: bool) -> None:
            self.native_output_active = active

    window = Window()
    host.projection_session = SimpleNamespace(
        state={"type": "image"},
        projection_windows=(window,),
        all_windows=lambda: (window,),
    )
    host._projection_targets = SimpleNamespace(
        restore_state_to_window=restored.append,
    )

    MainWindow._reconcile_native_scene_surfaces(cast(MainWindow, host))

    assert not window.native_output_active
    assert runtime.targets == ()
    assert restored == [window]


def test_audio_only_projection_keeps_the_native_idle_route_stable(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "NATIVE_SCENES_SUPPORTED", True)
    host, _projection_bar, runtime = _native_preview_host(requested=False)
    restored: list[object] = []

    class Window:
        native_output_active = True
        native_video_surface = object()

        def set_native_output_active(self, active: bool) -> None:
            self.native_output_active = active

    window = Window()
    host.projection_session = SimpleNamespace(
        state={"type": "video", "is_audio": True},
        projection_windows=(window,),
        all_windows=lambda: (window,),
    )
    host._projection_targets = SimpleNamespace(
        restore_state_to_window=restored.append,
    )

    MainWindow._reconcile_native_scene_surfaces(cast(MainWindow, host))

    assert window.native_output_active
    assert runtime.targets == (
        (window.native_video_surface, "media-window-0", BusId.MEDIA_WINDOWS),
    )
    assert restored == []


def test_audio_only_demand_does_not_republish_a_stale_video_frame() -> None:
    class Frame:
        def isValid(self) -> bool:
            return True

    enabled: list[bool] = []
    submitted: list[object] = []
    frame = Frame()
    host = SimpleNamespace(
        _content_frame_ingress=SimpleNamespace(set_enabled=enabled.append),
        _program_content=SimpleNamespace(
            refresh=lambda: None,
            submit_frame=submitted.append,
        ),
        projection_session=SimpleNamespace(
            state={"type": "video", "is_audio": True},
            state_type="video",
        ),
        media_ctrl=SimpleNamespace(
            video_sink=SimpleNamespace(videoFrame=lambda: frame),
        ),
    )

    MainWindow._on_content_ingress_demand_changed(cast(MainWindow, host), True)

    assert enabled == [True]
    assert submitted == []


@pytest.mark.parametrize("libobs", [True, False], ids=["libobs", "native"])
@pytest.mark.parametrize("payload", ["qimage", "bgra", "nv12"])
def test_program_frames_reach_fallback_windows_and_skip_native_outputs(
    monkeypatch,
    libobs: bool,
    payload: str,
) -> None:
    monkeypatch.setattr(main_window, "libobs_scene_engine_selected", lambda: libobs)
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(0xFF112233)
    frame: QImage | VideoFrame = image
    conversions: list[VideoFrame] = []
    if payload != "qimage":
        frame = VideoFrame(
            sequence=1,
            presentation_timestamp_ns=0,
            duration_ns=16_666_667,
            produced_monotonic_ns=0,
            media_epoch=0,
            width=2,
            height=2,
            pixel_format=VideoPixelFormat(payload),
            pixels=(
                bytes(image.constBits())
                if payload == "bgra"
                else bytes([16, 16, 16, 16, 128, 128])
            ),
        )
    if payload == "nv12":
        # NV12 conversion belongs to the shared converter; this regression
        # verifies that the shell still delegates transported frames to it.
        def convert(transported: VideoFrame) -> QImage:
            conversions.append(transported)
            return image

        monkeypatch.setattr(main_window, "video_frame_to_image", convert)

    class Window:
        def __init__(self, native_output_active: bool) -> None:
            self.native_output_active = native_output_active
            self.frames: list[tuple[QImage, bool]] = []

        def show_image_from_qimage(self, received: QImage, *, cache_pixmap: bool) -> None:
            self.frames.append((received, cache_pixmap))

    fallback = Window(False)
    native = Window(True)
    previews: list[QImage] = []
    host = SimpleNamespace(
        _program_mirror_enabled=lambda: True,
        projection_session=SimpleNamespace(all_windows=lambda: (native, fallback)),
        proj_bar=SimpleNamespace(update_tab_live_preview=previews.append),
    )

    MainWindow._on_scene_program_frame(cast(MainWindow, host), frame)

    assert len(fallback.frames) == 1
    received, cache_pixmap = fallback.frames[0]
    assert received == image
    assert cache_pixmap is False
    assert native.frames == []
    assert len(previews) == 1
    assert previews[0] is received
    if payload == "nv12":
        assert conversions == [frame]


@pytest.mark.parametrize("mirror_enabled", [False, True])
def test_program_frames_are_not_delivered_when_unmirrored_or_null(
    mirror_enabled: bool,
) -> None:
    image = QImage()
    if not mirror_enabled:
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(0xFF112233)
    received: list[QImage] = []
    window = SimpleNamespace(
        native_output_active=False,
        show_image_from_qimage=lambda frame, **_options: received.append(frame),
    )
    previews: list[QImage] = []
    host = SimpleNamespace(
        _program_mirror_enabled=lambda: mirror_enabled,
        projection_session=SimpleNamespace(all_windows=lambda: (window,)),
        proj_bar=SimpleNamespace(update_tab_live_preview=previews.append),
    )

    MainWindow._on_scene_program_frame(cast(MainWindow, host), image)

    assert received == []
    assert previews == []


def test_active_program_recording_blocks_normal_window_close() -> None:
    warnings: list[tuple[str, str]] = []
    host = SimpleNamespace(
        _program_recording=SimpleNamespace(busy=True),
        notifications=SimpleNamespace(
            warning=lambda message, *, dedupe_key: warnings.append(
                (message, dedupe_key)
            )
        ),
        tr=lambda text: text,
    )

    assert MainWindow.confirm_close(cast(MainWindow, host)) is False
    assert warnings == [
        (
            "Stop recording before closing Solin.",
            "program-recording-blocks-close",
        )
    ]


def test_idle_program_recording_does_not_block_normal_window_close() -> None:
    host = SimpleNamespace(
        _program_recording=SimpleNamespace(busy=False),
        talk_theme_widget=SimpleNamespace(confirm_close=lambda: True),
    )

    assert MainWindow.confirm_close(cast(MainWindow, host)) is True


def test_recording_failure_only_claims_a_partial_file_when_one_exists(
    tmp_path,
) -> None:
    errors: list[str] = []
    host = SimpleNamespace(
        _last_program_recording_status=ProgramRecordingStatus.IDLE,
        notifications=SimpleNamespace(
            error=lambda message, **_kwargs: errors.append(message)
        ),
        tr=lambda text: text,
    )
    output_path = tmp_path / "Solin recording.mp4"

    MainWindow._on_program_recording_state_changed(
        cast(MainWindow, host),
        ProgramRecordingState(
            status=ProgramRecordingStatus.FAILED,
            output_path=output_path,
            error_code="recording_failed",
        ),
    )
    output_path.with_suffix(".mp4.part").write_bytes(b"partial")
    host._last_program_recording_status = ProgramRecordingStatus.IDLE
    MainWindow._on_program_recording_state_changed(
        cast(MainWindow, host),
        ProgramRecordingState(
            status=ProgramRecordingStatus.FAILED,
            output_path=output_path,
            error_code="recording_failed",
        ),
    )

    assert errors == [
        "Recording failed.",
        (
            "Recording failed. The file was preserved at "
            f"{output_path.with_suffix('.mp4.part')}"
        ),
    ]
