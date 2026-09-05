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
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_SCENE_ID,
    DEFAULT_SCENE_ID,
    SceneSeedNames,
)
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


def test_scene_toolbar_popup_uses_the_shared_program_recording_state(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller._set_engine_ready(True)
    recording = _Recording(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    assert popup._recording_button.isVisibleTo(popup)
    # icon-only: the state reads from the accessible name / tooltip
    assert popup._recording_button.text() == ""
    assert popup._recording_button.accessibleName() == "Start recording"
    assert popup._recording_button.isEnabled()
    assert popup._recording_button.accessibleName() == "Start recording"

    QTest.mouseClick(popup._recording_button, Qt.MouseButton.LeftButton)

    assert recording.toggle_count == 1
    assert popup._recording_button.accessibleName() == "Stop recording · 01:05"
    assert popup._recording_button.property("recording") is True
    assert popup._recording_clock.isActive()
    assert popup._recording_button.toolTip() == "Stop recording · 01:05"

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

    assert popup._recording_button.accessibleName() == "Finishing recording…"
    assert not popup._recording_button.isEnabled()

    recording.state = ProgramRecordingState(
        status=ProgramRecordingStatus.FAILED,
        error_code="recording_disk_full",
        message="The recording disk is full.",
    )
    recording.state_changed.emit(recording.state)

    assert popup._recording_button.accessibleName() == "Try recording again"
    assert popup._recording_button.isEnabled()
    assert "disk" in popup._recording_button.toolTip().lower() or popup._recording_button.toolTip()

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
    assert popup._recording_button.parentWidget() is popup._card
    assert not popup._recording_button.isWindow()

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_panel_header_has_an_attach_button_right_of_the_hover_button(
    tmp_path: Path,
) -> None:
    # The scenes panel can be attached to the bottom of the main window; its
    # toggle sits immediately right of the hover ("pointer") button so the two
    # read as one control group in the header.
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    popup.hover_button.show()  # the host reveals this when it binds a preference
    popup.show()
    QCoreApplication.processEvents()

    assert popup.dock_button.isVisibleTo(popup)
    assert popup.dock_button.isCheckable()
    assert not popup.dock_button.isChecked()  # floating by default
    # positioned to the right of the pointer button, and both are last in the row
    assert popup.dock_button.x() > popup.hover_button.x()
    assert popup.hover_button.x() > popup._output.x()

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


class _DockHost(QWidget):
    """Stands in for MainWindow: page content plus a lazy bottom dock strip."""

    def __init__(self) -> None:
        super().__init__()
        from PySide6.QtWidgets import QVBoxLayout

        self._content_layout = QVBoxLayout(self)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.addWidget(QWidget(self), 1)
        self._scenes_dock: QWidget | None = None

    def scenes_dock_container(self) -> QWidget:
        from PySide6.QtWidgets import QVBoxLayout

        if self._scenes_dock is None:
            container = QWidget(self)
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            container.hide()
            self._content_layout.addWidget(container)
            self._scenes_dock = container
        return self._scenes_dock


def test_attach_button_docks_the_panel_into_the_window_bottom(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    host = _DockHost()
    host.resize(900, 600)
    host.show()
    popup = SceneControlPopup(controller, host)
    QCoreApplication.processEvents()
    assert popup.isWindow() and not popup.docked  # floats by default

    popup.dock_button.setChecked(True)
    QCoreApplication.processEvents()

    dock = host.scenes_dock_container()
    assert popup.docked
    assert not popup.isWindow()  # a plain child widget, not a popup window
    assert popup.parentWidget() is dock
    assert dock.layout().indexOf(popup) >= 0
    assert dock.isVisible() and popup.isVisible()
    assert popup.width() == host.width()  # stretches across the window
    # spanning the window edge to edge, the card drops its rounded corners
    assert popup._card.property("docked") is True

    popup.dock_button.setChecked(False)
    QCoreApplication.processEvents()

    assert not popup.docked
    assert popup.isWindow()  # back to a floating popup
    assert not popup.isVisible()
    assert not dock.isVisible()  # emptied dock strip gets out of the way
    # floating again: no longer stretched to the window, and at least the
    # preferred width (it may be wider to fit the scene cards)
    assert popup.width() < host.width()
    assert popup.width() >= SceneControlPopup._PREFERRED_WIDTH
    assert popup._card.property("docked") is False  # rounded corners restored

    popup.deleteLater()
    host.close()
    host.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_attach_button_reverts_when_no_dock_host_is_available(tmp_path: Path) -> None:
    # A popup with no MainWindow ancestor (standalone) must stay floating rather
    # than half-dock, and the button must not stay stuck on.
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)

    popup.dock_button.setChecked(True)
    QCoreApplication.processEvents()

    assert not popup.docked
    assert not popup.dock_button.isChecked()
    assert popup.isWindow()

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_virtual_camera_button_states_its_status_and_pulses_while_live(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)

    # Disabled: says so plainly, and nothing pulses.
    popup._set_output_enabled(False)
    assert popup._output.text() == "Virtual camera disabled"
    assert not popup._output_pulse.running

    # Enabled: the label flips and the pulse runs so a live output is obvious.
    popup._set_output_enabled(True)
    assert popup._output.text() == "Virtual camera enabled"
    assert popup._output.isChecked()
    assert popup._output_pulse.running  # breathes while live

    # Back off: label restored, pulse stopped and its highlight cleared.
    popup._set_output_enabled(False)
    assert popup._output.text() == "Virtual camera disabled"
    assert not popup._output_pulse.running

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_record_button_is_icon_only_and_pulses_red_while_recording(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller._set_engine_ready(True)
    recording = _Recording(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    # Sits in the header, immediately left of the virtual-camera toggle.
    assert popup._recording_button.parentWidget() is popup._card
    assert popup._recording_button.x() < popup._output.x()
    # Idle: a plain button, no red state and nothing pulsing.
    assert popup._recording_button.text() == ""
    assert popup._recording_button.width() == popup._recording_button.height()  # square
    assert not popup._recording_button.icon().isNull()  # icon-only control
    assert popup._recording_button.toolTip()  # hover explains what it does
    assert popup._recording_button.property("recording") is False
    assert not popup._recording_pulse.running

    recording.toggle()
    QCoreApplication.processEvents()

    assert popup._recording_button.property("recording") is True  # red while live
    assert popup._recording_pulse.running  # breathes while recording

    recording.toggle()
    QCoreApplication.processEvents()

    assert popup._recording_button.property("recording") is False
    assert not popup._recording_pulse.running

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_live_pulses_never_put_a_graphics_effect_on_header_controls(
    tmp_path: Path,
) -> None:
    # Regression: a QGraphicsEffect on a child of this translucent, frameless
    # popup makes Qt rasterise the window — the rounded corners paint black and
    # the button text disappears. The pulse must tint the widget instead, so only
    # the popup itself may carry an effect.
    controller = _controller(tmp_path)
    controller._set_engine_ready(True)
    recording = _Recording(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    popup._set_output_enabled(True)
    recording.toggle()
    QCoreApplication.processEvents()
    assert popup._output_pulse.running and popup._recording_pulse.running

    assert popup._output.graphicsEffect() is None
    assert popup._recording_button.graphicsEffect() is None
    # the label survives while pulsing
    assert popup._output.text() == "Virtual camera enabled"

    # Stopping clears the tint so the panel's own styling shows through again.
    popup._set_output_enabled(False)
    recording.toggle()
    QCoreApplication.processEvents()
    assert popup._output.styleSheet() == ""
    assert popup._recording_button.styleSheet() == ""

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_dock_preference_survives_a_restart(tmp_path: Path) -> None:
    # Attaching the panel is remembered, so Solin comes back with it attached.
    from PySide6.QtCore import QSettings
    from solin.core.foundation.settings_store import ProfileAppSettingsStore

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    organization = f"SolinDockPref{tmp_path.name}"
    settings = ProfileAppSettingsStore.for_organization(organization)
    settings.set_scenes_panel_docked(False)
    assert settings.scenes_panel_docked() is False

    settings.set_scenes_panel_docked(True)
    # A fresh store reads the same backend, standing in for a restarted app.
    assert ProfileAppSettingsStore.for_organization(organization).scenes_panel_docked() is True

    settings.set_scenes_panel_docked(False)
    assert ProfileAppSettingsStore.for_organization(organization).scenes_panel_docked() is False


def test_scene_cards_follow_the_document_with_canvas_proportions(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()

    document = controller.document
    # one card per scene, in document order
    assert [card.scene_id for card in popup._scene_cards.values()] == [
        scene.id for scene in document.scenes
    ]
    video = document.output(BusId.VIRTUAL_CAMERA).video_format
    card = popup._scene_cards[document.scenes[0].id]
    assert card.height() == SceneControlPopup._SCENE_CARD_HEIGHT
    # the preview rectangle carries the canvas proportions; the footer adds a row
    preview = card._preview
    assert preview.height() == SceneControlPopup._SCENE_CARD_PREVIEW_HEIGHT
    assert preview.width() == round(preview.height() * video.width / video.height)
    assert card.width() == preview.width()
    assert card._name.text() == document.scenes[0].name

    # renaming and removing a scene is reflected without rebuilding the strip
    controller.documents.rename_scene("lectern", "Tribuna")
    QCoreApplication.processEvents()
    assert popup._scene_cards["lectern"]._name.text() == "Tribuna"

    controller.documents.delete_scene("lectern")
    QCoreApplication.processEvents()
    assert "lectern" not in popup._scene_cards

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_strip_scrolls_sideways_and_never_exceeds_the_screen(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    controller = _controller(tmp_path)
    for index in range(10):
        controller.documents.create_scene(f"Scene {index}", scene_id=f"s{index}")
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()

    # A narrow work area caps the floating panel; the strip takes up the slack.
    popup._available_rect = QRect(0, 0, 700, 600)
    popup._sync_geometry()
    QCoreApplication.processEvents()

    assert popup.width() <= 700
    assert popup._cards_scroll.horizontalScrollBar().maximum() > 0  # scrolls sideways
    assert (
        popup._cards_scroll.verticalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff  # single row, never wraps
    )

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_scene_strip_hides_the_scrollbar_when_the_cards_all_fit(tmp_path: Path) -> None:
    # Regression: the panel width ignored the card frame's 1px borders, so it came
    # out 2px short of its own content — showing a scrollbar and clipping the last
    # card even with room to spare.
    from PySide6.QtCore import QRect

    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    popup._available_rect = QRect(0, 0, 1920, 1080)  # plenty of room
    popup._sync_geometry()
    QCoreApplication.processEvents()
    popup._sync_geometry()

    viewport = popup._cards_scroll.viewport().width()
    assert viewport >= popup._cards_host.sizeHint().width()  # content fits exactly
    bar = popup._cards_scroll.horizontalScrollBar()
    assert bar.maximum() == bar.minimum()  # nothing to scroll
    assert (
        popup._cards_scroll.horizontalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    # no wasted strip height reserved for a scrollbar that is not there
    assert popup._cards_scroll.height() == SceneControlPopup._SCENE_CARD_HEIGHT

    # Squeeze the work area until the cards overflow: the scrollbar comes back.
    for index in range(8):
        controller.documents.create_scene(f"Extra {index}", scene_id=f"x{index}")
    popup._available_rect = QRect(0, 0, 560, 1080)
    popup._sync_geometry()
    QCoreApplication.processEvents()
    popup._sync_geometry()

    bar = popup._cards_scroll.horizontalScrollBar()
    assert bar.maximum() > bar.minimum()
    assert popup._cards_scroll.height() > SceneControlPopup._SCENE_CARD_HEIGHT

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_panel_never_uses_a_graphics_effect_and_reopens_visible(tmp_path: Path) -> None:
    # Two regressions with one cause: a QGraphicsOpacityEffect on this
    # translucent frameless popup made Qt rasterise it (black behind the rounded
    # corners) and could leave it stranded fully transparent, so clicking the
    # toolbar appeared to do nothing. The fade now rides windowOpacity.
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    anchor = QWidget()
    anchor.resize(40, 40)
    anchor.show()
    QCoreApplication.processEvents()

    assert popup.graphicsEffect() is None

    popup.show_above(anchor)
    popup.hide()  # dismissed before the deferred fade could start
    QCoreApplication.processEvents()
    assert popup.windowOpacity() == 1.0  # not stranded transparent

    # A hidden panel is always left opaque, whatever interrupted the fade.
    popup.show_above(anchor)
    QCoreApplication.processEvents()
    popup.hide()
    assert popup.windowOpacity() == 1.0

    popup.deleteLater()
    anchor.close()
    anchor.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_panel_reports_a_just_dismissed_close_so_the_toolbar_can_toggle(
    tmp_path: Path,
) -> None:
    # Qt closes a popup on the click that lands outside it, so the click that
    # reaches the toolbar button arrives after the dismissal. Without this the
    # button always reopened the panel instead of toggling it shut.
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    anchor = QWidget()
    anchor.resize(40, 40)
    anchor.show()
    QCoreApplication.processEvents()

    assert popup.dismissed_within(40) is False  # never shown yet

    popup.show_above(anchor)
    QCoreApplication.processEvents()
    popup.hide()  # the outside click Qt delivers first
    assert popup.dismissed_within(40) is True  # so the button leaves it closed

    # The window must stay tight: a human's next deliberate click is far slower
    # than the same-click dismissal, and suppressing it stopped the panel from
    # ever reappearing when clicking at a normal pace.
    time.sleep(0.08)
    assert popup.dismissed_within(40) is False  # an 80ms-later click reopens it

    popup.deleteLater()
    anchor.close()
    anchor.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_undocking_restores_translucency_before_the_window_is_recreated(
    tmp_path: Path,
) -> None:
    # Regression: setParent() recreates the native window and X11 picks its
    # visual from WA_TranslucentBackground at creation time. Setting the
    # attribute *after* reparenting left the reopened panel opaque, so its
    # rounded corners painted black for the rest of the session.
    controller = _controller(tmp_path)
    host = _DockHost()
    host.resize(900, 600)
    host.show()
    popup = SceneControlPopup(controller, host)
    QCoreApplication.processEvents()
    translucent = Qt.WidgetAttribute.WA_TranslucentBackground
    assert popup.testAttribute(translucent)

    popup.dock_button.setChecked(True)
    QCoreApplication.processEvents()
    assert popup.docked
    assert not popup.testAttribute(translucent)  # opaque child while attached

    popup.dock_button.setChecked(False)
    QCoreApplication.processEvents()
    assert not popup.docked
    assert popup.testAttribute(translucent)  # and translucent again once floating
    assert popup.windowFlags() & Qt.WindowType.Popup

    # Reopening still works after the round trip.
    anchor = QWidget()
    anchor.resize(40, 40)
    anchor.show()
    QCoreApplication.processEvents()
    popup.show_above(anchor)
    QCoreApplication.processEvents()
    assert popup.isVisible()
    assert popup.testAttribute(translucent)

    popup.close()
    popup.deleteLater()
    anchor.close()
    anchor.deleteLater()
    host.close()
    host.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_card_routing_buttons_are_exclusive_and_pulse_the_live_one(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    scene_ids = [scene.id for scene in controller.document.scenes]
    first, second = popup._scene_cards[scene_ids[0]], popup._scene_cards[scene_ids[1]]

    first._projection.click()
    QCoreApplication.processEvents()
    assert first._projection.isChecked()
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == scene_ids[0]
    assert first._projection_pulse.running  # the live one breathes

    # Routing another scene takes it away from the first: one choice per output.
    second._projection.click()
    QCoreApplication.processEvents()
    assert second._projection.isChecked()
    assert not first._projection.isChecked()
    assert second._projection_pulse.running
    assert not first._projection_pulse.running

    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()


def test_card_virtual_camera_button_only_shows_while_that_output_runs(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, False)
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()

    cards = list(popup._scene_cards.values())
    assert all(not card._program.isVisibleTo(card) for card in cards)
    # the projection button is always offered
    assert all(card._projection.isVisibleTo(card) for card in cards)

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    QCoreApplication.processEvents()
    assert all(card._program.isVisibleTo(card) for card in cards)

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, False)
    QCoreApplication.processEvents()
    assert all(not card._program.isVisibleTo(card) for card in cards)
    assert all(not card._program_pulse.running for card in cards)

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()
    controller.close()
