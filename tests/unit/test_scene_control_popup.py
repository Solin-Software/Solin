from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Signal
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.projection.application import ProjectionSession
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    BusId,
    OutputMode,
    SceneLayer,
)
from solin.core.scenes.presets import (
    SceneSeedNames,
)
from solin.core.scenes.recording import (
    AudioDeviceDiscovery,
    ProgramRecordingState,
    ProgramRecordingStatus,
    SceneRecordingConfig,
)
from solin.core.scenes.repository import SceneRuntimeRepository
from solin.core.scenes.runtime import SceneRuntimeState
from solin.styles.theme import PALETTE
from solin.widgets.scenes import control_popup
from solin.widgets.scenes.control_popup import SceneControlPopup, _SceneCard
from tests._qt import wait_until


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

    def __init__(self, directory: Path, *, monotonic: Callable[[], float]) -> None:
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
        self._monotonic = monotonic
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
                started_at_monotonic=self._monotonic() - 65,
                output_path=self.directory / "recording.mp4",
                active_config=self.configuration,
            )
        )
        self.state_changed.emit(self.state)

    def effective_output_directory(self) -> Path:
        return self.directory


@pytest.fixture
def recording_factory(monkeypatch: pytest.MonkeyPatch):
    # Both sides use the same clock, independent of machine uptime. Replacing
    # this module binding leaves the shared time module and wait deadlines real.
    def monotonic() -> float:
        return 100.0

    monkeypatch.setattr(control_popup, "time", SimpleNamespace(monotonic=monotonic))

    def create(directory: Path) -> _Recording:
        return _Recording(directory, monotonic=monotonic)

    return create


@pytest.fixture
def controller_factory(request, tmp_path: Path, scene_workspace_factory):
    def create(*, projection: _Projection | None = None) -> SceneRuntimeController:
        paths = ProfilePaths.from_roots(
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            profile_id="scene-control-popup-test",
        )
        paths.ensure_dirs()
        workspace = scene_workspace_factory(
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
        controller = SceneRuntimeController(workspace, projection or _Projection())
        request.addfinalizer(controller.close)
        return controller

    return create


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
    controller_factory, recording_factory, tmp_path: Path,
) -> None:
    controller = controller_factory()
    controller._set_engine_ready(True)
    # Recording captures the virtual camera, so the control needs that output on.
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    recording = recording_factory(tmp_path / "Videos" / "Solin")
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
        started_at_monotonic=recording.state.started_at_monotonic,
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


def test_recording_controls_never_show_as_a_standalone_window_during_build(
    controller_factory, recording_factory, tmp_path: Path,
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

    controller = controller_factory()
    recording = recording_factory(tmp_path / "Videos" / "Solin")
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


def test_panel_header_has_an_attach_button_right_of_the_hover_button(
    controller_factory,
) -> None:
    # The scenes panel can be attached to the bottom of the main window; its
    # toggle sits immediately right of the hover ("pointer") button so the two
    # read as one control group in the header.
    controller = controller_factory()
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


def test_attach_button_docks_the_panel_into_the_window_bottom(controller_factory) -> None:
    controller = controller_factory()
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


def test_attach_button_reverts_when_no_dock_host_is_available(controller_factory) -> None:
    # A popup with no MainWindow ancestor (standalone) must stay floating rather
    # than half-dock, and the button must not stay stuck on.
    controller = controller_factory()
    popup = SceneControlPopup(controller)

    popup.dock_button.setChecked(True)
    QCoreApplication.processEvents()

    assert not popup.docked
    assert not popup.dock_button.isChecked()
    assert popup.isWindow()

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_virtual_camera_button_states_its_status_with_static_live_style(
    controller_factory,
) -> None:
    controller = controller_factory()
    popup = SceneControlPopup(controller)

    # Disabled: the label and checked state describe the output.
    popup._set_output_enabled(False)
    assert popup._output.text() == "Virtual camera disabled"
    assert not popup._output.isChecked()

    # Enabled: the shared stylesheet supplies the static checked highlight.
    popup._set_output_enabled(True)
    assert popup._output.text() == "Virtual camera enabled"
    assert popup._output.isChecked()
    assert popup._output.styleSheet() == ""

    # Back off: restore the inactive state.
    popup._set_output_enabled(False)
    assert popup._output.text() == "Virtual camera disabled"
    assert not popup._output.isChecked()

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_record_button_is_icon_only_with_static_red_recording_style(
    controller_factory, recording_factory, tmp_path: Path,
) -> None:
    controller = controller_factory()
    controller._set_engine_ready(True)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    recording = recording_factory(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    # Sits in the header, immediately left of the virtual-camera toggle.
    assert popup._recording_button.parentWidget() is popup._card
    assert popup._recording_button.x() < popup._output.x()
    # Idle: a plain button with no recording highlight.
    assert popup._recording_button.text() == ""
    assert popup._recording_button.width() == popup._recording_button.height()  # square
    assert not popup._recording_button.icon().isNull()  # icon-only control
    assert popup._recording_button.toolTip()  # hover explains what it does
    assert popup._recording_button.property("recording") is False
    assert popup._recording_button.styleSheet() == ""

    recording.toggle()
    QCoreApplication.processEvents()

    assert popup._recording_button.property("recording") is True  # red while live
    assert popup._recording_button.styleSheet() == ""

    recording.toggle()
    QCoreApplication.processEvents()

    assert popup._recording_button.property("recording") is False
    assert popup._recording_button.styleSheet() == ""

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_live_controls_use_static_styles_without_graphics_effects(
    controller_factory, recording_factory, tmp_path: Path,
) -> None:
    # Regression: a QGraphicsEffect on a child of this translucent, frameless
    # popup makes Qt rasterise the window — the rounded corners paint black and
    # the button text disappears. Live controls use the panel's static stylesheet.
    from PySide6.QtCore import QVariantAnimation
    controller = controller_factory()
    controller._set_engine_ready(True)
    recording = recording_factory(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    popup._set_output_enabled(True)
    recording.toggle()
    QCoreApplication.processEvents()
    assert popup._output.isChecked()
    assert popup._recording_button.property("recording") is True
    assert popup._output.styleSheet() == ""
    assert popup._recording_button.styleSheet() == ""
    assert all(animation.loopCount() != -1 for animation in popup.findChildren(QVariantAnimation))

    assert popup._output.graphicsEffect() is None
    assert popup._recording_button.graphicsEffect() is None
    # The active label remains visible.
    assert popup._output.text() == "Virtual camera enabled"

    # Both states continue to use the panel's own stylesheet.
    popup._set_output_enabled(False)
    recording.toggle()
    QCoreApplication.processEvents()
    assert popup._output.styleSheet() == ""
    assert popup._recording_button.styleSheet() == ""

    popup.deleteLater()
    QCoreApplication.processEvents()


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


def test_scene_cards_follow_the_document_with_canvas_proportions(controller_factory) -> None:
    controller = controller_factory()
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
    assert card.width() == preview.width() + 2 * _SceneCard._BORDER_WIDTH
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


def test_scene_strip_scrolls_sideways_and_never_exceeds_the_screen(controller_factory) -> None:

    controller = controller_factory()
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


def test_scene_strip_hides_the_scrollbar_when_the_cards_all_fit(controller_factory) -> None:
    # Regression: the panel width ignored the card frame's 1px borders, so it came
    # out 2px short of its own content — showing a scrollbar and clipping the last
    # card even with room to spare.

    controller = controller_factory()
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


def test_panel_never_uses_a_graphics_effect_and_reopens_visible(controller_factory) -> None:
    # Two regressions with one cause: a QGraphicsOpacityEffect on this
    # translucent frameless popup made Qt rasterise it (black behind the rounded
    # corners) and could leave it stranded fully transparent, so clicking the
    # toolbar appeared to do nothing. The fade now rides windowOpacity.
    controller = controller_factory()
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


def test_panel_reports_a_just_dismissed_close_so_the_toolbar_can_toggle(
    controller_factory,
) -> None:
    # Qt closes a popup on the click that lands outside it, so the click that
    # reaches the toolbar button arrives after the dismissal. Without this the
    # button always reopened the panel instead of toggling it shut.
    controller = controller_factory()
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


def test_undocking_restores_translucency_before_the_window_is_recreated(
    controller_factory,
) -> None:
    # Regression: setParent() recreates the native window and X11 picks its
    # visual from WA_TranslucentBackground at creation time. Setting the
    # attribute *after* reparenting left the reopened panel opaque, so its
    # rounded corners painted black for the rest of the session.
    controller = controller_factory()
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


def test_card_routing_buttons_are_exclusive_with_static_live_style(controller_factory) -> None:
    controller = controller_factory()
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    scene_ids = [scene.id for scene in controller.document.scenes]
    first, second = popup._scene_cards[scene_ids[0]], popup._scene_cards[scene_ids[1]]

    first._projection.click()
    QCoreApplication.processEvents()
    assert first._projection.isChecked()
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == scene_ids[0]
    assert first._projection.styleSheet() == ""

    # Routing another scene takes it away from the first: one choice per output.
    second._projection.click()
    QCoreApplication.processEvents()
    assert second._projection.isChecked()
    assert not first._projection.isChecked()
    assert second._projection.styleSheet() == ""
    assert first._projection.styleSheet() == ""

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_card_virtual_camera_button_only_shows_while_that_output_runs(
    controller_factory,
) -> None:
    controller = controller_factory()
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
    assert all(card._program.styleSheet() == "" for card in cards)

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_panel_requests_thumbnails_only_while_it_is_on_screen(controller_factory) -> None:
    # Thumbnails cost GPU renders in the sidecar, so a closed panel must not ask
    # for them.
    controller = controller_factory()
    asked: list[tuple] = []
    controller.set_thumbnail_egress = lambda d, ids, w, h: asked.append((d, ids, w, h))
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    popup._sync_geometry()
    popup._sync_thumbnail_feed()

    assert asked, "no thumbnail feed requested while visible"
    descriptor, scene_ids, width, height = asked[-1]
    assert descriptor is not None
    assert scene_ids == tuple(popup._scene_cards)
    # the atlas cell matches the card's preview, so rows map 1:1 onto cards
    preview = next(iter(popup._scene_cards.values()))._preview
    assert (width, height) == (preview.width(), preview.height())
    assert descriptor.height == height * len(scene_ids)

    popup.hide()
    QCoreApplication.processEvents()
    assert asked[-1] == (None, (), 0, 0)  # released on hide

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_scene_card_paints_the_live_thumbnail_it_is_given(controller_factory) -> None:
    from PySide6.QtGui import QImage

    controller = controller_factory()
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    scene_id = next(iter(popup._scene_cards))
    card = popup._scene_cards[scene_id]

    image = QImage(card._preview.width(), card._preview.height(), QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)
    popup._on_thumbnail(scene_id, image)

    painted = card._preview.grab().toImage()
    centre = painted.pixelColor(painted.width() // 2, painted.height() // 2)
    assert (centre.red(), centre.green(), centre.blue()) == (0, 255, 0)

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_thumbnail_request_is_retried_until_the_engine_hears_it(controller_factory) -> None:
    # Regression: the panel opens before the engine is ready, the controller drops
    # the command, and because the block had already been allocated every later
    # sync short-circuited — so the cards stayed dead for the whole session.
    controller = controller_factory()
    ready = {"value": False}
    calls: list[tuple] = []

    def _set(descriptor, scene_ids, width, height):
        calls.append((descriptor, scene_ids, width, height))
        return ready["value"]  # False = engine not ready, nothing dispatched

    controller.set_thumbnail_egress = _set
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    popup._sync_geometry()

    popup._sync_thumbnail_feed()
    assert calls, "nothing attempted"
    assert popup._thumbnail_sent is None  # not recorded: the engine never heard it

    popup._sync_thumbnail_feed()
    assert len(calls) >= 2, "a dropped request must be retried"

    ready["value"] = True
    popup._sync_thumbnail_feed()
    assert popup._thumbnail_sent is not None  # recorded only once delivered

    before = len(calls)
    popup._sync_thumbnail_feed()
    assert len(calls) == before  # and not re-sent forever afterwards

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_card_buttons_route_each_output_independently(controller_factory) -> None:
    # Regression: the card handler was left calling take_program_scene, the old
    # lockstep take, so routing the projection dragged the virtual camera with it
    # (and the reverse). The outputs are independent.
    controller = controller_factory()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    scene_ids = [scene.id for scene in controller.document.scenes]
    projection_card = popup._scene_cards[scene_ids[1]]
    program_card = popup._scene_cards[scene_ids[2]]

    projection_card._projection.click()
    QCoreApplication.processEvents()
    # routing the projection must leave the program exactly where it was
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == scene_ids[1]
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) != scene_ids[1]

    program_card._program.click()
    QCoreApplication.processEvents()

    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == scene_ids[1]
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == scene_ids[2]
    assert projection_card._projection.isChecked()
    assert not projection_card._program.isChecked()
    assert program_card._program.isChecked()
    assert not program_card._projection.isChecked()

    popup.deleteLater()
    QCoreApplication.processEvents()


@pytest.mark.parametrize("role", ["projection", "program"])
def test_card_right_click_requests_return_without_left_routing(role: str) -> None:
    card = _SceneCard("camera", "Camera")
    routed: list[tuple[str, str]] = []
    returns: list[tuple[str, str]] = []
    card.routing_requested.connect(lambda scene_id, role: routed.append((scene_id, role)))
    button = card._projection if role == "projection" else card._program

    try:
        card.routing_return_requested.connect(
            lambda scene_id, role: returns.append((scene_id, role))
        )
        QTest.mouseClick(button, Qt.MouseButton.RightButton)
        QTest.mouseClick(button, Qt.MouseButton.RightButton)

        assert returns == [("camera", role), ("camera", role)]
        assert routed == []
        assert not button.isChecked()

        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert routed == [("camera", role)]
        assert button.isChecked()
        QTest.mouseClick(button, Qt.MouseButton.RightButton)
        assert returns == [("camera", role)] * 3
        assert routed == [("camera", role)]
        assert button.isChecked()
    finally:
        card.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("docked", [False, True])
@pytest.mark.parametrize("has_content", [False, True])
@pytest.mark.parametrize(
    ("role", "bus_id"),
    [("projection", BusId.MEDIA_WINDOWS), ("program", BusId.VIRTUAL_CAMERA)],
)
def test_card_left_click_during_playback_returns_only_from_content_scenes(
    controller_factory, role: str, bus_id: BusId, docked: bool, has_content: bool,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    camera_scene = next(scene.id for scene in controller.document.scenes if scene.name == "Camera")
    controller.documents.create_scene("Lectern", scene_id="lectern")
    _add_camera_pip_scene(controller, "camera-pip")
    selected_scene = "camera-pip" if has_content else "lectern"
    controller.select_scene(bus_id, camera_scene)
    host = _DockHost()
    popup = SceneControlPopup(controller, host)
    popup.set_docked(docked)
    card = popup._scene_cards[selected_scene]
    button = card._projection if role == "projection" else card._program
    projection.set_type("video")
    return_before = controller.runtime.state.output(bus_id).manual_scene_id
    other_bus = BusId.VIRTUAL_CAMERA if bus_id is BusId.MEDIA_WINDOWS else BusId.MEDIA_WINDOWS
    other_before = controller.desired_scene(other_bus)

    try:
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

        assert controller.desired_scene(bus_id) == selected_scene
        assert controller.desired_scene(other_bus) == other_before
        expected_return = return_before if has_content else selected_scene
        assert controller.runtime.state.output(bus_id).manual_scene_id == expected_return
        assert controller.return_scene_override_available(bus_id) is has_content
        assert button.isChecked()
        assert button.styleSheet() == ""
        assert popup._scene_cards[selected_scene] is card

        projection.set_type("idle")
        assert controller.desired_scene(bus_id) == expected_return
        assert controller.runtime.state.output(bus_id).mode is OutputMode.AUTO
        projection.set_type("video")
        assert controller.desired_scene(bus_id) == controller.documents.program_media_scene_id
        assert controller.desired_scene(other_bus) == other_before
        projection.set_type("idle")
        assert controller.desired_scene(bus_id) == expected_return
    finally:
        popup.close()
        popup.deleteLater()
        host.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("docked", [False, True])
@pytest.mark.parametrize("override_return", [False, True])
@pytest.mark.parametrize(
    ("role", "bus_id"),
    [("projection", BusId.MEDIA_WINDOWS), ("program", BusId.VIRTUAL_CAMERA)],
)
def test_reselecting_content_from_a_card_restores_the_session_return(
    controller_factory, role: str, bus_id: BusId, docked: bool, override_return: bool,
) -> None:
    projection = ProjectionSession()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    camera_scene = next(scene.id for scene in controller.document.scenes if scene.name == "Camera")
    controller.select_scene(bus_id, camera_scene)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    controller.documents.create_scene("Fallback", scene_id="fallback")
    _add_camera_pip_scene(controller, "camera-pip")
    host = _DockHost()
    popup = SceneControlPopup(controller, host)
    popup.set_docked(docked)
    content_card = popup._scene_cards["camera-pip"]
    content_button = content_card._projection if role == "projection" else content_card._program
    fallback_card = popup._scene_cards["fallback"]
    fallback_button = fallback_card._projection if role == "projection" else fallback_card._program
    lectern_card = popup._scene_cards["lectern"]
    lectern_button = lectern_card._projection if role == "projection" else lectern_card._program
    camera_card = popup._scene_cards[camera_scene]
    camera_button = camera_card._projection if role == "projection" else camera_card._program
    other_bus = BusId.VIRTUAL_CAMERA if bus_id is BusId.MEDIA_WINDOWS else BusId.MEDIA_WINDOWS
    projection.set_state({"type": "video", "path": "clip.mp4"})
    other_runtime = controller.runtime.state.output(other_bus)
    other_desired = controller.desired_scene(other_bus)

    try:
        QTest.mouseClick(content_button, Qt.MouseButton.LeftButton)
        assert controller.desired_scene(bus_id) == "camera-pip"
        if override_return:
            QTest.mouseClick(fallback_button, Qt.MouseButton.RightButton)
            assert controller.desired_scene(bus_id) == "camera-pip"
            assert PALETTE.success in fallback_button.styleSheet()
            wait_until(
                lambda: not popup._success_flash._active,
                description="return-scene feedback completion",
            )
        return_scene = "fallback" if override_return else camera_scene

        QTest.mouseClick(lectern_button, Qt.MouseButton.LeftButton)
        assert controller.desired_scene(bus_id) == "lectern"
        assert controller.runtime.state.output(bus_id).manual_scene_id == "lectern"
        assert not controller.return_scene_override_available(bus_id)
        before_runtime, before_desired = controller.runtime.state, controller.desired_scenes
        QTest.mouseClick(camera_button, Qt.MouseButton.RightButton)
        assert controller.runtime.state is before_runtime
        assert controller.desired_scenes == before_desired
        assert camera_button.styleSheet() == ""
        assert popup._success_flash._active == {}
        assert lectern_button.isChecked()

        QTest.mouseClick(content_button, Qt.MouseButton.LeftButton)
        assert controller.desired_scene(bus_id) == "camera-pip"
        assert controller.runtime.state.output(bus_id).manual_scene_id == return_scene
        assert content_button.isChecked()
        projection.update_state(position=20, paused=True)
        assert controller.desired_scene(bus_id) == "camera-pip"
        assert controller.desired_scene(other_bus) == other_desired
        assert controller.runtime.state.output(other_bus) == other_runtime
        projection.set_state({"type": "idle"})
        assert controller.desired_scene(bus_id) == return_scene
        assert controller.runtime.state.output(other_bus) == other_runtime
        assert popup._scene_cards["camera-pip"] is content_card
    finally:
        popup.close()
        popup.deleteLater()
        host.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("docked", [False, True])
@pytest.mark.parametrize(
    ("role", "bus_id"),
    [("projection", BusId.MEDIA_WINDOWS), ("program", BusId.VIRTUAL_CAMERA)],
)
def test_card_right_click_saves_only_its_output_return_and_restores_feedback(
    controller_factory, monkeypatch: pytest.MonkeyPatch, role: str, bus_id: BusId, docked: bool,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    host = _DockHost()
    popup = SceneControlPopup(controller, host)
    popup.set_docked(docked)
    card = popup._scene_cards["lectern"]
    button = card._projection if role == "projection" else card._program
    other_button = card._program if role == "projection" else card._projection
    button.setStyleSheet(f"QPushButton#{button.objectName()} {{ padding:0; }}")
    original_style = button.styleSheet()
    projection.set_type("video")
    controller._set_applied_scenes(controller.desired_scenes)
    before_desired, before_applied = controller.desired_scenes, controller.applied_scenes
    before_runtime = controller.runtime.state
    before_checked = button.isChecked()
    other_bus = BusId.VIRTUAL_CAMERA if bus_id is BusId.MEDIA_WINDOWS else BusId.MEDIA_WINDOWS

    try:
        assert controller.return_scene_override_available(bus_id)
        save_return = controller.set_return_scene
        saved: list[tuple[BusId, str]] = []

        def save(output: BusId, scene_id: str):
            assert button.styleSheet() == original_style
            state = save_return(output, scene_id)
            saved.append((output, scene_id))
            return state

        with monkeypatch.context() as patch:
            patch.setattr(controller, "set_return_scene", save)
            QTest.mouseClick(button, Qt.MouseButton.RightButton)
        assert saved == [(bus_id, "lectern")]
        assert PALETTE.success in button.styleSheet()
        assert other_button.styleSheet() == ""

        # A second success restarts the short flash on the same live card.
        QTest.mouseClick(button, Qt.MouseButton.RightButton)
        assert controller.desired_scenes == before_desired
        assert controller.applied_scenes == before_applied
        assert controller.runtime.state.output(bus_id).manual_scene_id == "lectern"
        assert controller.runtime.state.output(other_bus) == before_runtime.output(other_bus)
        assert button.isChecked() == before_checked
        assert popup._scene_cards["lectern"] is card
        assert len(popup._success_flash._active) == 1

        # Rendering and docking reuse the card while feedback is running.
        popup.set_docked(not docked)
        popup._render()
        assert popup._scene_cards["lectern"] is card
        wait_until(
            lambda: not popup._success_flash._active,
            description="return-scene feedback completion",
        )
        assert button.styleSheet() == original_style
        assert popup._success_flash._active == {}

        projection.set_type("idle")
        assert controller.desired_scene(bus_id) == "lectern"
        assert controller.desired_scene(other_bus) != "lectern"
        projection.set_type("video")
        assert controller.desired_scene(bus_id) != "lectern"
        projection.set_type("idle")
        assert controller.desired_scene(bus_id) == "lectern"
    finally:
        popup.close()
        popup.deleteLater()
        host.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize(
    ("role", "bus_id"),
    [("projection", BusId.MEDIA_WINDOWS), ("program", BusId.VIRTUAL_CAMERA)],
)
@pytest.mark.parametrize("outcome", ["denied", "save_failed", "media_scene"])
def test_card_rejected_return_has_no_success_flash_or_routing_changes(
    controller_factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    role: str, bus_id: BusId, outcome: str,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    popup = SceneControlPopup(controller)
    projection.set_type("video")
    controller._set_applied_scenes(controller.desired_scenes)
    scene_id = (
        controller.documents.program_media_scene_id if outcome == "media_scene"
        else next(scene.id for scene in controller.document.scenes if scene.name == "Camera")
    )
    card = popup._scene_cards[scene_id]
    button = card._projection if role == "projection" else card._program
    original_style = button.styleSheet()
    before_desired, before_applied = controller.desired_scenes, controller.applied_scenes
    before_runtime = controller.runtime.state
    before_checked = button.isChecked()
    availability_calls: list[BusId] = []
    save_calls: list[tuple[BusId, str]] = []

    def available(output: BusId) -> bool:
        availability_calls.append(output)
        return outcome != "denied"

    def fail_save(output: BusId, target: str):
        save_calls.append((output, target))
        raise OSError("Return scene save failed")

    if outcome != "media_scene":
        monkeypatch.setattr(
            popup, "_controller",
            Mock(
                wraps=controller,
                return_scene_override_available=available,
                set_return_scene=fail_save,
            ),
        )

    try:
        QTest.mouseClick(button, Qt.MouseButton.RightButton)
        if outcome != "media_scene":
            assert availability_calls == [bus_id]
        assert save_calls == ([(bus_id, scene_id)] if outcome == "save_failed" else [])
        assert button.styleSheet() == original_style
        assert popup._success_flash._active == {}
        assert controller.desired_scenes == before_desired
        assert controller.applied_scenes == before_applied
        assert controller.runtime.state == before_runtime
        assert button.isChecked() == before_checked
        if outcome != "denied":
            assert "Could not set the return scene from its card" in caplog.text
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("has_content", [False, True])
def test_disabled_projection_card_allows_return_override_only_from_content(
    controller_factory, has_content: bool,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.MEDIA_WINDOWS, False)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    _add_camera_pip_scene(controller, "camera-pip")
    selected_scene = "camera-pip" if has_content else "lectern"
    popup = SceneControlPopup(controller)
    projection.set_type("video")
    camera_scene = next(scene.id for scene in controller.document.scenes if scene.name == "Camera")
    button = popup._scene_cards[camera_scene]._projection

    try:
        QTest.mouseClick(popup._scene_cards[selected_scene]._projection, Qt.MouseButton.LeftButton)
        assert controller.desired_scene(BusId.MEDIA_WINDOWS) == selected_scene
        assert controller.applied_scenes == ()
        assert controller.return_scene_override_available(BusId.MEDIA_WINDOWS) is has_content
        before_runtime = controller.runtime.state

        QTest.mouseClick(button, Qt.MouseButton.RightButton)
        assert controller.desired_scene(BusId.MEDIA_WINDOWS) == selected_scene
        if has_content:
            assert PALETTE.success in button.styleSheet()
        else:
            assert button.styleSheet() == ""
            assert popup._success_flash._active == {}
            assert controller.runtime.state is before_runtime
        projection.set_type("idle")
        assert controller.desired_scene(BusId.MEDIA_WINDOWS) == (
            camera_scene if has_content else selected_scene
        )
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize(
    ("role", "bus_id"),
    [("projection", BusId.MEDIA_WINDOWS), ("program", BusId.VIRTUAL_CAMERA)],
)
def test_failed_noncontent_card_take_preserves_live_media_and_saved_return(
    controller_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    role: str, bus_id: BusId,
) -> None:
    projection = ProjectionSession()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    camera_scene = next(scene.id for scene in controller.document.scenes if scene.name == "Camera")
    controller.select_scene(bus_id, camera_scene)
    controller.documents.create_scene("Lectern", scene_id="lectern")
    _add_camera_pip_scene(controller, "camera-pip")
    popup = SceneControlPopup(controller)
    projection.set_state({"type": "video", "path": "clip.mp4"})
    content_card = popup._scene_cards["camera-pip"]
    content_button = content_card._projection if role == "projection" else content_card._program
    QTest.mouseClick(content_button, Qt.MouseButton.LeftButton)
    controller._set_applied_scenes(controller.desired_scenes)
    card = popup._scene_cards["lectern"]
    button = card._projection if role == "projection" else card._program
    before_runtime = controller.runtime.state
    before_desired, before_applied = controller.desired_scenes, controller.applied_scenes
    repository = SceneRuntimeRepository(tmp_path / "saved-runtime.json")
    repository.save(before_runtime)
    attempts: list[SceneRuntimeState] = []

    def fail_save(state: SceneRuntimeState, *, expected_revision: int | None = None) -> None:
        attempts.append(state)
        raise OSError("Runtime save failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller.runtime, "_store", repository)
            patch.setattr(repository, "save", fail_save)
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert len(attempts) == 1
            assert attempts[0].output(bus_id).manual_scene_id == "lectern"
            assert "Could not route the scene from its card" in caplog.text
            assert controller.runtime.state is before_runtime
            assert repository.load_or_create(controller.document) == before_runtime
            assert controller.desired_scenes == before_desired
            assert controller.applied_scenes == before_applied
            assert not button.isChecked()
            assert content_button.isChecked()
            assert popup._success_flash._active == {}
            projection.update_state(position=20, paused=True)
            assert controller.desired_scenes == before_desired
        projection.set_state({"type": "idle"})
        assert controller.desired_scene(bus_id) == camera_scene
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


def test_failed_automation_resume_preserves_temporary_card_routing(
    controller_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = ProjectionSession()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    _add_camera_pip_scene(controller, "camera-pip")
    controller.documents.create_scene("Lectern", scene_id="lectern")
    popup = SceneControlPopup(controller)
    projection.set_state({"type": "video", "path": "clip.mp4"})
    card = popup._scene_cards["camera-pip"]
    QTest.mouseClick(card._program, Qt.MouseButton.LeftButton)
    controller.take_scene(BusId.MEDIA_WINDOWS, "lectern")
    controller._set_applied_scenes(controller.desired_scenes)
    before_runtime = controller.runtime.state
    before_desired, before_applied = controller.desired_scenes, controller.applied_scenes
    repository = SceneRuntimeRepository(tmp_path / "saved-runtime.json")
    repository.save(before_runtime)
    attempts: list[SceneRuntimeState] = []

    def fail_save(state: SceneRuntimeState, *, expected_revision: int | None = None) -> None:
        attempts.append(state)
        raise OSError("Runtime save failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller.runtime, "_store", repository)
            patch.setattr(repository, "save", fail_save)
            with pytest.raises(OSError, match="Runtime save failed"):
                controller.resume_program_automation()
            assert len(attempts) == 1
            assert controller.runtime.state is before_runtime
            assert repository.load_or_create(controller.document) == before_runtime
            assert controller.desired_scenes == before_desired
            assert controller.applied_scenes == before_applied

            # A same-session update must still resolve the operator's live take.
            projection.update_state(position=20, paused=True)
            assert controller.desired_scenes == before_desired
            assert card._program.isChecked()
            assert popup._scene_cards["camera-pip"] is card
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


def test_failed_automation_enable_never_persists_an_intermediate_return_base(
    controller_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = ProjectionSession()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    _add_camera_pip_scene(controller, "camera-pip")
    controller.documents.create_scene("Lectern", scene_id="lectern")
    popup = SceneControlPopup(controller)
    projection.set_state({"type": "video", "path": "clip.mp4"})
    controller.take_program_scene("lectern")
    controller.take_scene(BusId.MEDIA_WINDOWS, "camera-pip")
    before_runtime = controller.runtime.state
    before_desired = controller.desired_scenes
    repository = SceneRuntimeRepository(tmp_path / "saved-runtime.json")
    repository.save(before_runtime)
    save = repository.save
    attempts: list[SceneRuntimeState] = []

    def reject_auto_save(
        state: SceneRuntimeState, *, expected_revision: int | None = None,
    ) -> None:
        attempts.append(state)
        if all(output.mode is OutputMode.AUTO for output in state.outputs):
            raise OSError("Automatic mode save failed")
        save(state, expected_revision=expected_revision)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller.runtime, "_store", repository)
            patch.setattr(repository, "save", reject_auto_save)
            with pytest.raises(OSError, match="Automatic mode save failed"):
                controller.set_program_automatic(True)
            assert controller.runtime.state is before_runtime
            assert repository.load_or_create(controller.document) == before_runtime
            assert len(attempts) == 1

            projection.update_state(position=20)
            assert controller.desired_scenes == before_desired
            assert popup._scene_cards["camera-pip"]._projection.isChecked()
            assert popup._scene_cards["lectern"]._program.isChecked()
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


@pytest.mark.parametrize("stop_before_resuming", [False, True])
def test_automatic_off_on_preserves_independent_card_returns_and_saved_bases(
    controller_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_before_resuming: bool,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    _add_camera_pip_scene(controller, "camera-pip")
    controller.documents.create_scene("Lectern", scene_id="lectern")
    popup = SceneControlPopup(controller)
    projection_card = popup._scene_cards["camera-pip"]
    program_card = popup._scene_cards["lectern"]
    QTest.mouseClick(projection_card._projection, Qt.MouseButton.LeftButton)
    QTest.mouseClick(program_card._program, Qt.MouseButton.LeftButton)
    before_runtime = controller.runtime.state
    repository = SceneRuntimeRepository(tmp_path / "saved-runtime.json")
    repository.save(before_runtime)
    projection.set_type("video")
    media_desired = controller.desired_scenes

    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller.runtime, "_store", repository)
            controller.set_program_automatic(False)
            assert controller.desired_scenes == media_desired
            assert all(output.mode is OutputMode.MANUAL for output in controller.runtime.state.outputs)
            if stop_before_resuming:
                projection.set_type("idle")
                assert controller.desired_scenes == media_desired

            controller.set_program_automatic(True)
            if not stop_before_resuming:
                assert controller.desired_scenes == media_desired
            assert controller.runtime.state.outputs == before_runtime.outputs
            restored = repository.load_or_create(controller.document)
            assert restored == controller.runtime.state
            assert restored.output(BusId.MEDIA_WINDOWS).manual_scene_id == "camera-pip"
            assert restored.output(BusId.VIRTUAL_CAMERA).manual_scene_id == "lectern"

            projection.set_type("idle")
            assert controller.desired_scene(BusId.MEDIA_WINDOWS) == "camera-pip"
            assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == "lectern"
            assert projection_card._projection.isChecked()
            assert program_card._program.isChecked()
            assert popup._scene_cards["camera-pip"] is projection_card
            assert popup._scene_cards["lectern"] is program_card
    finally:
        popup.deleteLater()
        QCoreApplication.processEvents()


def test_live_thumbnail_is_clipped_to_the_card_corners(controller_factory) -> None:
    from PySide6.QtGui import QImage

    controller = controller_factory()
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    scene_id = next(iter(popup._scene_cards))
    card = popup._scene_cards[scene_id]

    image = QImage(card._preview.width(), card._preview.height(), QImage.Format.Format_ARGB32)
    image.fill(0xFFFF00FF)  # magenta
    popup._on_thumbnail(scene_id, image)

    painted = card.grab().toImage()
    magenta = (255, 0, 255)

    def pixel(x: int, y: int) -> tuple:
        colour = painted.pixelColor(x, y)
        return (colour.red(), colour.green(), colour.blue())

    # the feed fills the preview but must not square off the card's rounded top
    assert pixel(painted.width() // 2, 30) == magenta
    assert pixel(1, 1) != magenta
    assert pixel(painted.width() - 2, 1) != magenta

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_choosing_a_scene_from_a_card_keeps_media_auto_switch_working(
    controller_factory,
) -> None:
    # Regression: the card pinned its output to MANUAL, so media stopped taking
    # the output over — the video played but was never shown — and the panel no
    # longer has the auto-switch control that used to undo that.
    projection = _Projection()
    controller = controller_factory(projection=projection)
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    camera_scene = next(
        scene.id for scene in controller.document.scenes if scene.name == "Camera"
    )
    media_scene = controller.documents.program_media_scene_id

    popup._scene_cards[camera_scene]._projection.click()
    QCoreApplication.processEvents()
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == camera_scene

    projection.set_type("video")
    QCoreApplication.processEvents()
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == media_scene  # media wins

    projection.set_type("idle")
    QCoreApplication.processEvents()
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == camera_scene  # and returns

    popup.deleteLater()
    QCoreApplication.processEvents()


def test_idle_media_slot_shows_a_faded_glyph_instead_of_pure_black(
    controller_factory,
) -> None:
    projection = _Projection()
    controller = controller_factory(projection=projection)
    popup = SceneControlPopup(controller)
    popup.show()
    QCoreApplication.processEvents()
    popup._render()

    content_scene = controller.documents.program_media_scene_id
    camera_scene = next(
        scene.id for scene in controller.document.scenes if scene.name == "Camera"
    )
    # only the scene that actually holds the media slot is marked
    assert popup._scene_cards[content_scene]._preview._placeholder
    assert not popup._scene_cards[camera_scene]._preview._placeholder

    # Something really is painted: the same preview differs with the hint off.
    # (Comparing whole renders rather than one pixel — the glyph is an outline,
    # so its exact centre is hollow.)
    preview = popup._scene_cards[content_scene]._preview
    with_hint = preview.grab().toImage()
    preview.set_placeholder(False)
    without_hint = preview.grab().toImage()
    assert with_hint != without_hint
    preview.set_placeholder(True)

    projection.set_type("video")
    popup._render()
    assert not popup._scene_cards[content_scene]._preview._placeholder

    popup.close()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_the_record_button_only_exists_while_the_virtual_camera_does(
    controller_factory, recording_factory, tmp_path: Path,
) -> None:
    """Recording captures Program, so there is nothing to record with the output off."""
    controller = controller_factory()
    controller._set_engine_ready(True)
    recording = recording_factory(tmp_path / "Videos" / "Solin")
    popup = SceneControlPopup(controller, recording=recording)

    assert not popup._recording_button.isVisibleTo(popup)

    popup._set_output_enabled(True)
    assert popup._recording_button.isVisibleTo(popup)

    popup._set_output_enabled(False)
    assert not popup._recording_button.isVisibleTo(popup)
    assert not popup._recording_clock.isActive()

    popup.deleteLater()
    QCoreApplication.processEvents()
