from types import SimpleNamespace
from typing import cast

import solin.main_window as main_window
from solin.core.scenes.model import BusId
from solin.main_window import MainWindow, _use_native_media_presentation


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

    @property
    def python_video_frame_delivery_required(self) -> bool:
        return not self.active_states or not self.active_states[-1]


class _MediaController:
    def __init__(self) -> None:
        self.delivery_requirements: list[bool] = []

    def set_python_frame_delivery_required(self, required: bool) -> None:
        self.delivery_requirements.append(required)


class _SceneRuntime:
    native_window_routing_ready = True

    def __init__(self) -> None:
        self.targets = ()

    def set_window_targets(self, targets) -> None:
        self.targets = targets


def _native_preview_host(*, requested: bool):
    projection_bar = _ProjectionBar(requested=requested)
    runtime = _SceneRuntime()
    media_controller = _MediaController()
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
        _content_frame_ingress=SimpleNamespace(direct_submission_active=True),
        media_ctrl=media_controller,
    )
    host._raw_projection_windows = lambda: MainWindow._raw_projection_windows(
        cast(MainWindow, host)
    )
    host._reconcile_python_video_frame_delivery = (
        lambda: MainWindow._reconcile_python_video_frame_delivery(
            cast(MainWindow, host)
        )
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


def test_direct_submission_skips_python_when_every_surface_is_native() -> None:
    host, projection_bar, _runtime = _native_preview_host(requested=True)
    projection_bar.active_states.append(True)

    MainWindow._reconcile_python_video_frame_delivery(cast(MainWindow, host))

    assert host.media_ctrl.delivery_requirements == [False]


def test_direct_submission_keeps_python_for_a_qt_preview() -> None:
    host, projection_bar, _runtime = _native_preview_host(requested=True)
    projection_bar.active_states.append(False)

    MainWindow._reconcile_python_video_frame_delivery(cast(MainWindow, host))

    assert host.media_ctrl.delivery_requirements == [True]


def test_direct_submission_keeps_python_for_a_fallback_window() -> None:
    host, projection_bar, _runtime = _native_preview_host(requested=True)
    projection_bar.active_states.append(True)
    fallback_window = SimpleNamespace(native_output_active=False)
    host.projection_session.all_windows = lambda: (fallback_window,)

    MainWindow._reconcile_python_video_frame_delivery(cast(MainWindow, host))

    assert host.media_ctrl.delivery_requirements == [True]


def test_fallback_ingress_always_restores_python_delivery() -> None:
    host, projection_bar, _runtime = _native_preview_host(requested=True)
    projection_bar.active_states.append(True)
    host._content_frame_ingress.direct_submission_active = False

    MainWindow._reconcile_python_video_frame_delivery(cast(MainWindow, host))

    assert host.media_ctrl.delivery_requirements == [True]
