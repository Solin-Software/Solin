from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.model import BusId, OutputMode, TransitionKind, TransitionSpec
from solin.core.scenes.presets import SceneSeedNames
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.widgets.scenes.control_popup import SceneControlPopup


class _Projection:
    state = {"type": "idle"}

    def subscribe(self, _listener):
        return lambda: None


def _controller(tmp_path: Path) -> SceneRuntimeController:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="scene-control-popup-test",
    )
    paths.ensure_dirs()
    workspace = SceneWorkspaceService(
        paths,
        seed_names=SceneSeedNames(
            content_source="Current content",
            default_camera_source="Default camera",
            no_signal_source="No signal background",
            content_scene="Content",
            camera_scene="Camera",
            content_camera_pip_scene="Content and camera",
            no_signal_scene="No signal",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
    )
    return SceneRuntimeController(workspace, _Projection())


def test_scene_toolbar_popup_controls_one_program_and_two_destinations(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    target_scene_id = controller.document.scenes[1].id

    popup._take(target_scene_id)
    popup._set_output_enabled(True)
    popup._set_media_mirror_enabled(True)

    media = controller.runtime.state.output(BusId.MEDIA_WINDOWS)
    virtual = controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
    assert media.mode is OutputMode.AUTO
    assert media.manual_scene_id == target_scene_id
    assert media.enabled
    assert virtual.mode is OutputMode.AUTO
    assert virtual.manual_scene_id == target_scene_id
    assert virtual.enabled
    assert popup._engine_status.text() == "Scene engine unavailable"

    controller._set_last_engine_error_code("invalid_scene_snapshot")
    popup._render()
    assert popup._engine_status.text() == (
        "Scene error: Scene engine files do not match this Solin version."
    )
    assert "invalid_scene_snapshot" in popup._engine_status.toolTip()

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_toolbar_popup_only_describes_transition_overrides(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    scene_id = controller.document.scenes[0].id
    controller.documents.set_scene_transition_override(
        scene_id,
        TransitionSpec(TransitionKind.DISSOLVE, 500),
    )

    popup = SceneControlPopup(controller)
    button = popup._list.findChildren(type(popup._output))[0]

    assert button.toolTip() == "Transition override: Dissolve · 500 ms"
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()
