from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Signal
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    BusId,
    OutputMode,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
)
from solin.core.scenes.presets import SceneSeedNames
from solin.core.scenes.recording import (
    AudioDeviceDiscovery,
    ProgramRecordingState,
    ProgramRecordingStatus,
    SceneRecordingConfig,
)
from solin.core.scenes.workspace import SceneWorkspaceService
from solin.widgets.scenes.control_popup import SceneControlPopup


class _Projection:
    def __init__(self) -> None:
        self.state = {"type": "idle"}
        self.session_id = 0
        self._listeners = set()

    def subscribe(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def set_type(self, state_type: str) -> None:
        self.state = {"type": state_type}
        self.session_id += 1
        for listener in tuple(self._listeners):
            listener()


class _Recording(QObject):
    state_changed = Signal(object)
    audio_devices_changed = Signal(object)
    configuration_changed = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.supported = True
        self.state = ProgramRecordingState()
        self.configuration = SceneRecordingConfig()
        self.audio_devices = AudioDeviceDiscovery(
            supported=True,
            ready=True,
            generation=1,
            devices=(),
        )
        self.directory = directory
        self.toggle_count = 0

    @property
    def busy(self) -> bool:
        return self.state.busy

    def toggle(self) -> None:
        self.toggle_count += 1
        self.state = (
            ProgramRecordingState()
            if self.busy
            else ProgramRecordingState(
                status=ProgramRecordingStatus.RECORDING,
                started_at_monotonic=time.monotonic() - 65,
                output_path=self.directory / "recording.mp4",
                active_config=self.configuration,
            )
        )
        self.state_changed.emit(self.state)

    def effective_output_directory(self) -> Path:
        return self.directory


def _controller(
    tmp_path: Path,
    *,
    projection: _Projection | None = None,
) -> SceneRuntimeController:
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
    return SceneRuntimeController(workspace, projection or _Projection())


def _add_camera_pip_scene(
    controller: SceneRuntimeController,
    scene_id: str,
    *,
    camera_source_id: str = DEFAULT_CAMERA_SOURCE_ID,
) -> None:
    controller.documents.create_scene("Camera PiP", scene_id=scene_id)
    controller.documents.add_layer(
        scene_id,
        SceneLayer(
            id=f"{scene_id}-content",
            source_id=CONTENT_SOURCE_ID,
            name="Content",
        ),
    )
    controller.documents.add_layer(
        scene_id,
        SceneLayer(
            id=f"{scene_id}-camera",
            source_id=camera_source_id,
            name="Camera",
        ),
    )


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
    assert popup._engine_visual_state == "unavailable"
    assert popup._icon.toolTip() == "Unavailable"
    assert popup._info_full_text == "Unavailable"
    assert popup._info.isVisibleTo(popup)
    assert popup._info.height() == 16
    assert popup._output.text() == "Virtual camera"
    initial_height = popup.height()
    popup._set_operation_error("Transient operation error")
    popup._render()
    assert popup._info_full_text == "Unavailable"
    assert popup._operation_error_timer.interval() == 5000

    controller._set_last_engine_error_code("invalid_scene_snapshot")
    popup._render()
    assert popup._engine_visual_state == "error"
    assert "files do not match" in popup._icon.toolTip()
    assert "invalid_scene_snapshot" in popup._icon.toolTip()
    assert "invalid_scene_snapshot" in popup._info_full_text
    assert popup.height() == initial_height

    controller._set_last_engine_error_code("")
    controller._set_engine_ready(True)
    popup._render()
    assert popup._info_full_text == "Transient operation error"
    popup._clear_operation_error()
    assert popup._info_full_text == "Scene engine ready"

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_toolbar_popup_uses_the_shared_program_recording_state(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller._set_engine_ready(True)
    recording = _Recording(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    assert popup._recording_row.isVisibleTo(popup)
    assert popup._recording_button.text() == "Start recording"
    assert popup._recording_button.isEnabled()
    assert popup._recording_button.accessibleName() == "Start recording"

    QTest.mouseClick(popup._recording_button, Qt.MouseButton.LeftButton)

    assert recording.toggle_count == 1
    assert popup._recording_button.text() == "Stop recording · 01:05"
    assert popup._recording_button.property("recording") is True
    assert popup._recording_clock.isActive()
    assert popup._recording_button.toolTip() == "Stop recording"

    recording.state = ProgramRecordingState(
        status=ProgramRecordingStatus.RECORDING,
        started_at_monotonic=time.monotonic() - 65,
        output_path=tmp_path / "recording.mp4",
        active_config=recording.configuration,
        microphone_warning="Microphone unavailable; recording silence.",
    )
    recording.state_changed.emit(recording.state)
    assert "Microphone unavailable" in popup._recording_button.toolTip()

    recording.state = ProgramRecordingState(
        status=ProgramRecordingStatus.STOPPING,
        output_path=tmp_path / "recording.mp4",
        active_config=recording.configuration,
    )
    recording.state_changed.emit(recording.state)

    assert popup._recording_button.text() == "Finishing recording…"
    assert not popup._recording_button.isEnabled()

    recording.state = ProgramRecordingState(
        status=ProgramRecordingStatus.FAILED,
        error_code="recording_disk_full",
        message="The recording disk is full.",
    )
    recording.state_changed.emit(recording.state)

    assert popup._recording_button.text() == "Try recording again"
    assert popup._recording_button.isEnabled()
    assert popup._info_full_text == (
        "The recording folder is unavailable or does not have enough free space."
    )

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_recording_controls_never_show_as_a_standalone_window_during_build(
    tmp_path: Path,
) -> None:
    class _TopLevelShowRecorder(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.object_names: list[str] = []

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
            if (
                event.type() is QEvent.Type.Show
                and isinstance(watched, QWidget)
                and watched.isWindow()
            ):
                self.object_names.append(watched.objectName())
            return False

    controller = _controller(tmp_path)
    recording = _Recording(tmp_path / "Videos" / "Solin")
    application = QApplication.instance()
    assert application is not None
    recorder = _TopLevelShowRecorder()
    application.installEventFilter(recorder)
    try:
        popup = SceneControlPopup(controller, recording=recording)
    finally:
        application.removeEventFilter(recorder)

    assert "SceneControlRecordingRow" not in recorder.object_names
    assert popup._recording_row.parentWidget() is popup._card
    assert not popup._recording_row.isWindow()

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
    button = popup._scene_rows[scene_id]

    assert "Transition override: Dissolve · 500 ms" in button.toolTip()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_chips_keep_identity_and_move_roles_out_of_visible_text(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    default_scene_id = controller.documents.program_default_scene_id
    assert default_scene_id is not None
    default_row = popup._scene_rows[default_scene_id]

    assert default_row.text() == "Camera"
    assert "DEFAULT" not in default_row.text()
    assert default_row.accessibleName() == "Camera"
    assert "Default scene" in default_row.accessibleDescription()
    assert "Default scene" in default_row.toolTip()

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)

    assert popup._scene_rows[default_scene_id] is default_row
    popup._render()
    assert popup._scene_rows[default_scene_id] is default_row
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_configured_scenes_are_pinned_in_scene_list_order(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Speaker + reader", scene_id="speaker-reader")
    controller.documents.create_scene("Audience overview", scene_id="audience-overview")
    default_scene_id = controller.documents.program_default_scene_id
    media_scene_id = controller.documents.program_media_scene_id
    assert default_scene_id is not None
    assert media_scene_id is not None
    controller.documents.reorder_scene(media_scene_id, 0)
    popup = SceneControlPopup(controller)

    assert popup._configured_cards.widgets == (
        popup._scene_rows[media_scene_id],
        popup._scene_rows[default_scene_id],
    )
    assert popup._other_cards.widgets == (
        popup._scene_rows["speaker-reader"],
        popup._scene_rows["audience-overview"],
    )
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_camera_pip_scenes_are_grouped_between_configured_and_other_scenes(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Before", scene_id="before")
    _add_camera_pip_scene(controller, "speaker-pip")
    controller.documents.create_scene("After", scene_id="after")

    popup = SceneControlPopup(controller)

    assert popup._pip_label.text() == "Camera PiP"
    assert popup._pip_cards.widgets == (popup._scene_rows["speaker-pip"],)
    assert popup._other_cards.widgets == (
        popup._scene_rows["before"],
        popup._scene_rows["after"],
    )
    assert popup._scene_order == (
        *(scene.id for scene in controller.document.scenes[:2]),
        "speaker-pip",
        "before",
        "after",
    )
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_configured_role_takes_precedence_over_camera_pip_group(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    _add_camera_pip_scene(controller, "configured-pip")
    controller.documents.set_program_media_scene("configured-pip")

    popup = SceneControlPopup(controller)

    assert popup._scene_rows["configured-pip"] in popup._configured_cards.widgets
    assert popup._pip_cards.widgets == ()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_camera_pip_group_follows_nested_camera_sources_and_live_edits(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    camera_scene_id = controller.documents.program_default_scene_id
    assert camera_scene_id is not None
    nested_source = SourceDefinition(
        id="nested-camera-source",
        kind=SourceKind.SCENE_REFERENCE,
        name="Nested camera",
        configuration=SceneReferenceConfig(target_scene_id=camera_scene_id),
    )
    controller.documents.create_source(nested_source)
    controller.documents.create_scene("Nested PiP", scene_id="nested-pip")
    popup = SceneControlPopup(controller)

    assert popup._pip_cards.widgets == ()
    assert popup._scene_rows["nested-pip"] in popup._other_cards.widgets

    controller.documents.add_layer(
        "nested-pip",
        SceneLayer(
            id="nested-pip-content",
            source_id=CONTENT_SOURCE_ID,
            name="Content",
        ),
    )
    controller.documents.add_layer(
        "nested-pip",
        SceneLayer(
            id="nested-pip-camera",
            source_id=nested_source.id,
            name="Nested camera",
        ),
    )
    QCoreApplication.processEvents()

    assert popup._pip_cards.widgets == (popup._scene_rows["nested-pip"],)
    assert popup._scene_rows["nested-pip"] not in popup._other_cards.widgets
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_camera_below_content_stays_in_other_scenes(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Camera below", scene_id="camera-below")
    controller.documents.add_layer(
        "camera-below",
        SceneLayer(
            id="camera-below-camera",
            source_id=DEFAULT_CAMERA_SOURCE_ID,
            name="Camera",
        ),
    )
    controller.documents.add_layer(
        "camera-below",
        SceneLayer(
            id="camera-below-content",
            source_id=CONTENT_SOURCE_ID,
            name="Content",
        ),
    )

    popup = SceneControlPopup(controller)

    assert popup._pip_cards.widgets == ()
    assert popup._other_cards.widgets == (popup._scene_rows["camera-below"],)
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_chips_reflow_after_a_live_document_update_without_collisions(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Media", scene_id="other-media")
    controller.documents.create_scene("Speaker", scene_id="speaker")
    controller.documents.create_scene("Reader", scene_id="reader")
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    stable_row = popup._scene_rows["speaker"]

    controller.documents.rename_scene("other-media", "Wide media presentation")
    QCoreApplication.processEvents()
    popup._sync_geometry()

    assert popup._scene_rows["speaker"] is stable_row
    assert all(widget.isVisible() for widget in popup._other_cards.widgets)
    geometries = [widget.geometry() for widget in popup._other_cards.widgets]
    for index, geometry in enumerate(geometries):
        for other in geometries[index + 1 :]:
            assert not geometry.intersects(other)
            if geometry.y() == other.y():
                left, right = sorted((geometry, other), key=lambda candidate: candidate.x())
                assert right.x() - left.right() - 1 >= 8

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_return_override_keeps_media_live_until_projection_ends(
    tmp_path: Path,
) -> None:
    projection = _Projection()
    controller = _controller(tmp_path, projection=projection)
    controller.documents.create_scene("Return scene", scene_id="return-scene")
    popup = SceneControlPopup(controller)
    media_scene_id = controller.documents.program_media_scene_id
    return_scene_id = "return-scene"
    assert media_scene_id is not None

    controller._set_engine_ready(True)
    projection.set_type("video")
    controller._set_applied_scenes(((BusId.VIRTUAL_CAMERA, media_scene_id),))
    popup._render()
    QTest.mousePress(
        popup._scene_rows[return_scene_id],
        Qt.MouseButton.RightButton,
    )

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == media_scene_id
    assert controller.program_return_scene_id == return_scene_id
    assert popup._info_full_text == "Return: Return scene · Right-click to change"
    assert popup._scene_rows[return_scene_id].styleSheet()

    projection.set_type("idle")

    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == return_scene_id
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_auto_switch_toggle_disables_automation_during_a_manual_override(
    tmp_path: Path,
) -> None:
    projection = _Projection()
    controller = _controller(tmp_path, projection=projection)
    popup = SceneControlPopup(controller)
    default_scene_id = controller.documents.program_default_scene_id
    assert default_scene_id is not None
    projection.set_type("video")
    popup._take(default_scene_id)
    assert controller.program_automation_suspended
    assert popup._automatic.isChecked()

    QTest.mouseClick(popup._automatic, Qt.MouseButton.LeftButton)

    assert controller.runtime.state.output(BusId.VIRTUAL_CAMERA).mode is OutputMode.MANUAL
    assert not controller.program_automation_suspended
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == default_scene_id
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_popup_smooth_scroll_accumulates_and_geometry_fits_work_area(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    bar = popup._scroll.verticalScrollBar()
    bar.setRange(0, 500)

    popup._scroll._animate_delta(80)
    popup._scroll._animate_delta(80)

    assert popup._scroll._scroll_animation.duration() == 140
    assert popup._scroll._scroll_animation.endValue() == 160

    popup._available_rect = QRect(0, 0, 360, 240)
    popup._anchor_rect = QRect(150, 200, 40, 24)
    popup._sync_geometry()

    assert popup.width() == 344
    assert popup.height() <= 224
    assert popup.geometry().left() >= 8
    assert popup.geometry().right() <= 351
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_popup_preserves_status_and_footer_on_a_very_short_work_area(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    popup._available_rect = QRect(0, 0, 260, 180)
    popup._anchor_rect = QRect(110, 140, 40, 24)

    popup._sync_geometry()

    assert popup.width() == 244
    assert popup.height() <= 164
    assert popup._scroll.geometry().bottom() < popup._info.geometry().top()
    assert popup._info.geometry().bottom() < popup._automatic.geometry().top()
    assert popup._media_mirror.geometry().bottom() <= popup._card.contentsRect().bottom()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_rows_support_arrow_and_home_end_keyboard_navigation(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    controller.documents.create_scene("Audience overview", scene_id="audience")
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    first, second, below = (popup._scene_rows[scene_id] for scene_id in popup._scene_order[:3])
    first.setFocus()

    QTest.keyClick(first, Qt.Key.Key_Down)
    assert below.hasFocus()

    QTest.keyClick(below, Qt.Key.Key_Up)
    assert first.hasFocus()

    QTest.keyClick(first, Qt.Key.Key_Right)
    assert second.hasFocus()

    QTest.keyClick(second, Qt.Key.Key_Home)
    assert first.hasFocus()

    QTest.keyClick(first, Qt.Key.Key_End)
    assert popup._scene_rows[popup._scene_order[-1]].hasFocus()
    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()
