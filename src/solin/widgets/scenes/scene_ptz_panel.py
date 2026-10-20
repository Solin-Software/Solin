"""PTZ controls for the cameras of one scene, beside its live window.

The panel acts on one camera at a time, chosen with chips (or Tab in the
window). Moving holds until release, re-sent while held as the editor's popup
does. Presets recall with visible feedback and are saved from where the camera
points now. A camera that is on air — through this scene or any other — asks
before each preset recall, and once before manual moves until it leaves the air.

Every control is mouse-only (no keyboard focus) so the window keeps the keys
for its shortcuts: arrows, +/−, Space, 1–9.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
import logging

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.scenes.composition import scene_ptz_camera_ids
from solin.core.scenes.model import BusId, CameraPreset
from solin.core.scenes.ptz import PendingCameraPreset, PtzControlResult, PtzRecallResult
from solin.styles.icons import (
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_CHEVRON_UP,
    ICON_REC_STOP,
    make_icon,
)
from solin.styles.theme import PALETTE, qss_rgba


log = logging.getLogger(__name__)

PANEL_WIDTH = 280
# ONVIF continuous moves expire on the camera after 0.75 s; re-send while held.
_MOTION_REPEAT_MS = 350
_DONE_FEEDBACK_MS = 2000
_DEFAULT_SPEED = 5  # of 10
_MOTION_KEYS = {
    Qt.Key.Key_Left.value: (-1.0, 0.0, 0.0),
    Qt.Key.Key_Right.value: (1.0, 0.0, 0.0),
    Qt.Key.Key_Up.value: (0.0, 1.0, 0.0),
    Qt.Key.Key_Down.value: (0.0, -1.0, 0.0),
    Qt.Key.Key_Plus.value: (0.0, 0.0, 1.0),
    Qt.Key.Key_Equal.value: (0.0, 0.0, 1.0),
    Qt.Key.Key_Minus.value: (0.0, 0.0, -1.0),
}

Confirm = Callable[[QWidget, str, str], bool]


def confirm_with_dialog(parent: QWidget, title: str, text: str) -> bool:
    answer = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


class _PresetRow(QPushButton):
    """One preset: its number and name, with recall feedback on the right."""

    context_requested = Signal(str)

    def __init__(self, preset_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preset_id = preset_id
        self.setObjectName("ScenePtzPreset")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _point: self.context_requested.emit(preset_id))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(8)
        self.label = QLabel()
        self.label.setObjectName("ScenePtzPresetName")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.label, 1)
        self.feedback = QLabel()
        self.feedback.setObjectName("ScenePtzPresetFeedback")
        self.feedback.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.feedback)
        self.setMinimumHeight(34)

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        self.setAccessibleName(text)

    def set_feedback(self, state: str, text: str) -> None:
        self.feedback.setText(text)
        self.setProperty("feedback", state)
        self.style().unpolish(self)
        self.style().polish(self)


class ScenePtzPanel(QWidget):
    """Pad, speed, and presets for the PTZ cameras one scene shows."""

    _ptz_result = Signal(object)  # (kind, camera_id, preset_id, result | None, extra)

    def __init__(
        self,
        controller: SceneRuntimeController,
        scene_id: str,
        *,
        speeds: dict[str, int] | None = None,
        confirm: Confirm = confirm_with_dialog,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ScenePtzPanel")
        self.setFixedWidth(PANEL_WIDTH)
        self._controller = controller
        self.scene_id = scene_id
        self._speeds = speeds if speeds is not None else {}
        self._confirm = confirm
        self._camera_ids: tuple[str, ...] = ()
        self._camera_id = ""
        self._unlocked: set[str] = set()
        self._moving: tuple[str, float, float, float] | None = None
        self._preset_feedback: dict[str, tuple[str, str]] = {}
        self._pending_saves: dict[int, PendingCameraPreset] = {}
        self._repeat = QTimer(self)
        self._repeat.setInterval(_MOTION_REPEAT_MS)
        self._repeat.timeout.connect(self._send_motion)
        self._ptz_result.connect(self._on_ptz_result, Qt.ConnectionType.QueuedConnection)
        self._build_ui()
        self.apply_theme()

    # ── layout ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self._camera_title = self._section_label()
        root.addWidget(self._camera_title)
        self._chips_host = QWidget()
        self._chips = QGridLayout(self._chips_host)
        self._chips.setContentsMargins(0, 0, 0, 0)
        self._chips.setSpacing(6)
        root.addWidget(self._chips_host)
        self._chip_buttons: dict[str, QPushButton] = {}
        self._also_in = QLabel()
        self._also_in.setObjectName("ScenePtzAlsoIn")
        self._also_in.setWordWrap(True)
        root.addWidget(self._also_in)
        self._live = QLabel()
        self._live.setObjectName("ScenePtzLive")
        self._live.setWordWrap(True)
        root.addWidget(self._live)

        pad_row = QHBoxLayout()
        pad_row.setSpacing(10)
        pad = QGridLayout()
        pad.setSpacing(4)
        self._pad_buttons: dict[str, QPushButton] = {}
        for name, icon, row, column, motion in (
            ("up", ICON_CHEVRON_UP, 0, 1, (0.0, 1.0, 0.0)),
            ("left", ICON_CHEVRON_LEFT, 1, 0, (-1.0, 0.0, 0.0)),
            ("right", ICON_CHEVRON_RIGHT, 1, 2, (1.0, 0.0, 0.0)),
            ("down", ICON_CHEVRON_DOWN, 2, 1, (0.0, -1.0, 0.0)),
        ):
            button = self._pad_button(f"ScenePtzMove_{name}", _svg(icon))
            button.pressed.connect(lambda motion=motion: self.start_motion(*motion))
            button.released.connect(self.stop_motion)
            pad.addWidget(button, row, column)
            self._pad_buttons[name] = button
        stop = self._pad_button("ScenePtzStop", _svg(ICON_REC_STOP))
        stop.clicked.connect(self.stop_motion)
        pad.addWidget(stop, 1, 1)
        self._pad_buttons["stop"] = stop
        pad_row.addLayout(pad)
        zoom = QVBoxLayout()
        zoom.setSpacing(4)
        for name, text, direction in (("zoom_in", "+", 1.0), ("zoom_out", "−", -1.0)):
            button = self._pad_button(f"ScenePtz_{name}", "")
            button.setText(text)
            button.pressed.connect(lambda direction=direction: self.start_motion(0.0, 0.0, direction))
            button.released.connect(self.stop_motion)
            zoom.addWidget(button)
            self._pad_buttons[name] = button
        pad_row.addLayout(zoom)
        pad_row.addStretch()
        root.addLayout(pad_row)

        speed_row = QHBoxLayout()
        speed_row.setSpacing(8)
        self._speed_label = QLabel()
        self._speed_label.setObjectName("ScenePtzMuted")
        speed_row.addWidget(self._speed_label)
        self._speed = QSlider(Qt.Orientation.Horizontal)
        self._speed.setObjectName("ScenePtzSpeed")
        self._speed.setRange(1, 10)
        self._speed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._speed.valueChanged.connect(self._remember_speed)
        speed_row.addWidget(self._speed, 1)
        root.addLayout(speed_row)

        presets_header = QHBoxLayout()
        self._presets_title = self._section_label()
        presets_header.addWidget(self._presets_title, 1)
        self._save = QPushButton()
        self._save.setObjectName("ScenePtzSave")
        self._save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save.clicked.connect(self.save_current_position)
        presets_header.addWidget(self._save)
        root.addLayout(presets_header)

        self._presets_scroll = QScrollArea()
        self._presets_scroll.setObjectName("ScenePtzPresets")
        self._presets_scroll.setWidgetResizable(True)
        self._presets_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._presets_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._presets_host = QWidget()
        self._presets_layout = QVBoxLayout(self._presets_host)
        self._presets_layout.setContentsMargins(0, 0, 0, 0)
        self._presets_layout.setSpacing(4)
        self._presets_layout.addStretch()
        self._presets_scroll.setWidget(self._presets_host)
        root.addWidget(self._presets_scroll, 1)
        self._preset_rows: dict[str, _PresetRow] = {}
        self._empty = QLabel()
        self._empty.setObjectName("ScenePtzMuted")
        self._empty.setWordWrap(True)
        self._presets_layout.insertWidget(0, self._empty)

        self._status = QLabel()
        self._status.setObjectName("ScenePtzStatus")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        root.addWidget(self._status)
        self._retranslate()

    def _section_label(self) -> QLabel:
        label = QLabel()
        label.setObjectName("ScenePtzSection")
        return label

    def _pad_button(self, object_name: str, icon: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setProperty("pad", True)
        button.setProperty("icon_svg", icon)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(40, 36)
        button.setIconSize(QSize(16, 16))
        return button

    def _retranslate(self) -> None:
        self._camera_title.setText(self.tr("Camera"))
        self._presets_title.setText(self.tr("Presets"))
        self._speed_label.setText(self.tr("Speed"))
        self._save.setText(self.tr("Save current"))
        self._save.setToolTip(self.tr("Save where this camera points now as a new preset"))
        self._empty.setText(self.tr("No presets for this camera yet."))
        for name, text in (
            ("up", self.tr("Move up")), ("down", self.tr("Move down")),
            ("left", self.tr("Move left")), ("right", self.tr("Move right")),
            ("stop", self.tr("Stop")), ("zoom_in", self.tr("Zoom in")),
            ("zoom_out", self.tr("Zoom out")),
        ):
            button = self._pad_buttons[name]
            button.setToolTip(text)
            button.setAccessibleName(text)

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._camera_ids

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def status_text(self) -> str:
        return self._status.text()

    def presets(self) -> tuple[CameraPreset, ...]:
        return tuple(
            preset for preset in self._controller.document.camera_presets
            if preset.camera_source_id == self._camera_id
        )

    def refresh(self) -> None:
        """Follow the document and the outputs: cameras, presets, and live state."""
        document = self._controller.document
        camera_ids = scene_ptz_camera_ids(document, self.scene_id)
        if camera_ids != self._camera_ids:
            self._camera_ids = camera_ids
            if self._camera_id not in camera_ids:
                self.stop_motion()
                self._camera_id = camera_ids[0] if camera_ids else ""
            self._rebuild_chips()
        for camera_id, chip in self._chip_buttons.items():
            chip.setText(self._camera_name(camera_id))
            chip.setChecked(camera_id == self._camera_id)
        self._speed.blockSignals(True)
        self._speed.setValue(self._speeds.get(self._camera_id, _DEFAULT_SPEED))
        self._speed.blockSignals(False)
        others = [
            scene.name for scene in document.scenes
            if scene.id != self.scene_id
            and self._camera_id in scene_ptz_camera_ids(document, scene.id)
        ]
        self._also_in.setText(
            self.tr("Also in: %1").replace("%1", ", ".join(others)) if others else ""
        )
        self._also_in.setVisible(bool(others))
        live = self.live_outputs(self._camera_id)
        for camera_id in tuple(self._unlocked):
            if not self.live_outputs(camera_id):
                self._unlocked.discard(camera_id)  # left the air: ask again next time
        if not live:
            self._live.setText("")
        elif self._camera_id in self._unlocked:
            self._live.setText(self.tr("On air · live control unlocked"))
        else:
            self._live.setText(self.tr("On air · moves are visible to the audience"))
        self._live.setVisible(bool(live))
        self.setProperty("live", bool(live))
        self.style().unpolish(self)
        self.style().polish(self)
        available = self._controller.ptz_controls_available and bool(self._camera_id)
        for button in self._pad_buttons.values():
            button.setEnabled(available)
        self._save.setEnabled(available)
        if not self._controller.ptz_controls_available:
            self._set_status(self.tr("PTZ control is unavailable in this installation."), error=True)
        self._rebuild_presets()

    def _camera_name(self, camera_id: str) -> str:
        source = next(
            (source for source in self._controller.document.sources if source.id == camera_id),
            None,
        )
        return source.name if source is not None else camera_id

    def _rebuild_chips(self) -> None:
        for chip in self._chip_buttons.values():
            self._chips.removeWidget(chip)
            chip.deleteLater()
        self._chip_buttons = {}
        for index, camera_id in enumerate(self._camera_ids):
            chip = QPushButton()
            chip.setObjectName("ScenePtzCamera")
            chip.setCheckable(True)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, camera_id=camera_id: self.select_camera(camera_id))
            self._chips.addWidget(chip, index // 2, index % 2)
            self._chip_buttons[camera_id] = chip
        self._chips_host.setVisible(len(self._camera_ids) > 0)

    def _rebuild_presets(self) -> None:
        presets = self.presets()
        wanted = [preset.id for preset in presets]
        for preset_id in tuple(self._preset_rows):
            if preset_id not in wanted:
                row = self._preset_rows.pop(preset_id)
                self._presets_layout.removeWidget(row)
                row.deleteLater()
        for index, preset in enumerate(presets):
            row = self._preset_rows.get(preset.id)
            if row is None:
                row = _PresetRow(preset.id)
                row.clicked.connect(lambda _checked=False, preset_id=preset.id: self.recall_preset(preset_id))
                row.context_requested.connect(self._show_preset_menu)
                self._preset_rows[preset.id] = row
            self._presets_layout.removeWidget(row)
            self._presets_layout.insertWidget(index + 1, row)
            number = f"{index + 1}  " if index < 9 else "    "
            row.set_text(f"{number}{preset.name}")
            state, text = self._preset_feedback.get(preset.id, ("", ""))
            row.set_feedback(state, text)
        self._empty.setVisible(not presets)

    def select_camera(self, camera_id: str) -> None:
        if camera_id not in self._camera_ids or camera_id == self._camera_id:
            self.refresh()
            return
        self.stop_motion()
        self._camera_id = camera_id
        self.refresh()

    def cycle_camera(self, step: int) -> None:
        if len(self._camera_ids) < 2:
            return
        index = self._camera_ids.index(self._camera_id) if self._camera_id in self._camera_ids else 0
        self.select_camera(self._camera_ids[(index + step) % len(self._camera_ids)])

    def live_outputs(self, camera_id: str) -> list[tuple[BusId, str]]:
        """Each output showing this camera now, with the scene that shows it."""
        if not camera_id:
            return []
        controller = self._controller
        document = controller.document
        buses = [BusId.MEDIA_WINDOWS]
        if controller.program_output_enabled:
            buses.insert(0, BusId.VIRTUAL_CAMERA)
        live: list[tuple[BusId, str]] = []
        for bus_id in buses:
            scene_id = controller.desired_scene(bus_id)
            if scene_id and camera_id in scene_ptz_camera_ids(document, scene_id):
                live.append((bus_id, scene_id))
        return live

    def _live_description(self, camera_id: str) -> str:
        names = {scene.id: scene.name for scene in self._controller.document.scenes}
        parts = []
        for bus_id, scene_id in self.live_outputs(camera_id):
            output = (
                self.tr("the virtual camera") if bus_id is BusId.VIRTUAL_CAMERA
                else self.tr("the projection")
            )
            parts.append(
                self.tr("%1 on %2").replace("%1", f"“{names.get(scene_id, scene_id)}”").replace("%2", output)
            )
        return "; ".join(parts)

    # ── motion ──────────────────────────────────────────────────────────────

    def start_motion(self, pan: float, tilt: float, zoom: float) -> None:
        camera_id = self._camera_id
        if not camera_id or not self._controller.ptz_controls_available:
            return
        if self.live_outputs(camera_id) and camera_id not in self._unlocked:
            text = self.tr(
                "%1 is on air: %2. Moving it changes what the audience sees. "
                "Allow moving it until it leaves the air?"
            ).replace("%1", f"“{self._camera_name(camera_id)}”").replace(
                "%2", self._live_description(camera_id)
            )
            if not self._confirm(self, self.tr("Camera on air"), text):
                self._release_pad()
                return
            self._unlocked.add(camera_id)
            self.refresh()
        self.stop_motion()
        self._moving = (camera_id, pan, tilt, zoom)
        self._send_motion()
        self._repeat.start()

    def _send_motion(self) -> None:
        if self._moving is None:
            self._repeat.stop()
            return
        camera_id, pan, tilt, zoom = self._moving
        speed = self._speeds.get(camera_id, _DEFAULT_SPEED) / 10
        try:
            future = self._controller.move_camera(
                camera_id, pan=pan, tilt=tilt, zoom=zoom, speed=max(0.1, speed),
            )
        except Exception:  # noqa: BLE001 - PTZ boundary
            log.warning("Could not move the camera", exc_info=True)
            self._set_status(self.tr("The camera could not be moved."), error=True)
            self.stop_motion()
            return
        self._observe(future, "control", camera_id, "")

    def stop_motion(self) -> None:
        self._repeat.stop()
        moving, self._moving = self._moving, None
        if moving is None:
            return
        try:
            future = self._controller.stop_camera(moving[0])
        except Exception:  # noqa: BLE001 - PTZ boundary
            log.warning("Could not stop the camera", exc_info=True)
            return
        self._observe(future, "control", moving[0], "")

    def _release_pad(self) -> None:
        for button in self._pad_buttons.values():
            if button.isDown():
                button.setDown(False)

    # ── presets ─────────────────────────────────────────────────────────────

    def recall_preset(self, preset_id: str) -> None:
        preset = next((item for item in self.presets() if item.id == preset_id), None)
        if preset is None or not self._controller.ptz_controls_available:
            return
        camera_id = preset.camera_source_id
        if self.live_outputs(camera_id):
            text = self.tr("%1 is on air: %2. Recall “%3” anyway?").replace(
                "%1", f"“{self._camera_name(camera_id)}”"
            ).replace("%2", self._live_description(camera_id)).replace("%3", preset.name)
            if not self._confirm(self, self.tr("Camera on air"), text):
                return
        self.stop_motion()
        try:
            future = self._controller.recall_camera_preset(preset_id)
        except Exception:  # noqa: BLE001 - PTZ boundary
            log.warning("Could not recall the preset", exc_info=True)
            self._set_feedback(preset_id, "error", self.tr("Failed"))
            return
        self._set_feedback(preset_id, "moving", self.tr("Moving…"))
        self._observe(future, "recall", camera_id, preset_id)

    def recall_preset_number(self, number: int) -> None:
        presets = self.presets()
        if 1 <= number <= min(9, len(presets)):
            self.recall_preset(presets[number - 1].id)

    def save_current_position(self) -> None:
        camera_id = self._camera_id
        if not camera_id or not self._controller.ptz_controls_available:
            return
        names = {preset.name for preset in self.presets()}
        number = len(names) + 1
        while self.tr("Preset %1").replace("%1", str(number)) in names:
            number += 1
        name = self.tr("Preset %1").replace("%1", str(number))
        try:
            preset, future = self._controller.store_current_camera_position(camera_id, name)
        except Exception:  # noqa: BLE001 - PTZ boundary
            log.warning("Could not store the camera position", exc_info=True)
            self._set_status(self.tr("The preset could not be saved."), error=True)
            return
        if preset is not None:
            self._pending_saves[id(future)] = preset
        self._set_status(self.tr("Saving “%1”…").replace("%1", name))
        self._observe(future, "save", camera_id, preset.id if preset is not None else "")

    def rename_preset(self, preset_id: str, name: str) -> bool:
        preset = next((item for item in self._controller.document.camera_presets if item.id == preset_id), None)
        name = name.strip()
        if preset is None or not name or name == preset.name:
            return False
        return self._edit(
            lambda: self._controller.workspace.update_camera_preset(preset_id, replace(preset, name=name))
        )

    def delete_preset(self, preset_id: str) -> bool:
        self._preset_feedback.pop(preset_id, None)
        return self._edit(lambda: self._controller.workspace.delete_camera_preset(preset_id))

    def _show_preset_menu(self, preset_id: str) -> None:
        preset = next((item for item in self.presets() if item.id == preset_id), None)
        row = self._preset_rows.get(preset_id)
        if preset is None or row is None:
            return
        menu = QMenu(self)
        rename = menu.addAction(self.tr("Rename…"))
        delete = menu.addAction(self.tr("Delete"))
        chosen = menu.exec(row.mapToGlobal(row.rect().bottomLeft()))
        if chosen is rename:
            name, accepted = QInputDialog.getText(
                self, self.tr("Rename preset"), self.tr("Preset name"), text=preset.name,
            )
            if accepted:
                self.rename_preset(preset_id, name)
        elif chosen is delete:
            text = self.tr("Delete the preset “%1”? Scenes that recall it stop doing so.").replace(
                "%1", preset.name
            )
            if self._confirm(self, self.tr("Delete preset"), text):
                self.delete_preset(preset_id)

    def _edit(self, operation: Callable[[], object]) -> bool:
        try:
            operation()
        except Exception:  # noqa: BLE001 - persistence boundary
            log.exception("Could not change the PTZ preset")
            self._set_status(self.tr("The preset could not be changed."), error=True)
            return False
        self.refresh()
        return True

    # ── results ─────────────────────────────────────────────────────────────

    def _observe(
        self,
        future: Future[PtzRecallResult] | Future[PtzControlResult],
        kind: str,
        camera_id: str,
        preset_id: str,
    ) -> None:
        def completed(done: Future) -> None:
            try:
                result = done.result()
            except Exception:  # noqa: BLE001 - async PTZ boundary
                result = None
            self._ptz_result.emit((kind, camera_id, preset_id, result, id(done)))

        future.add_done_callback(completed)

    def _on_ptz_result(self, payload: object) -> None:
        kind, camera_id, preset_id, result, future_key = payload  # type: ignore[misc]
        succeeded = bool(result is not None and result.succeeded)
        error_code = "" if result is None else str(result.error_code or "")
        if kind == "recall":
            if succeeded:
                self._set_feedback(preset_id, "done", "✓")
                QTimer.singleShot(_DONE_FEEDBACK_MS, lambda: self._clear_done(preset_id))
            else:
                self._set_feedback(preset_id, "error", self._short_error(error_code))
                self._set_status(self._long_error(error_code), error=True)
        elif kind == "save":
            pending = self._pending_saves.pop(future_key, None)
            if succeeded and pending is not None:
                token = str(getattr(result, "preset_token", "") or "")
                if self._edit(
                    lambda: self._controller.workspace.create_camera_preset(pending.stored(token))
                ):
                    self._set_status(self.tr("Saved “%1”.").replace("%1", pending.name))
            else:
                self._set_status(self._long_error(error_code), error=True)
        elif not succeeded and error_code not in {"", "ptz_command_superseded"}:
            self._set_status(self._long_error(error_code), error=True)

    def _set_feedback(self, preset_id: str, state: str, text: str) -> None:
        if state:
            self._preset_feedback[preset_id] = (state, text)
        else:
            self._preset_feedback.pop(preset_id, None)
        row = self._preset_rows.get(preset_id)
        if row is not None:
            row.set_feedback(state, text)

    def _clear_done(self, preset_id: str) -> None:
        if self._preset_feedback.get(preset_id, ("", ""))[0] == "done":
            self._set_feedback(preset_id, "", "")

    def preset_feedback(self, preset_id: str) -> tuple[str, str]:
        return self._preset_feedback.get(preset_id, ("", ""))

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setProperty("error", error)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    def _short_error(self, error_code: str) -> str:
        if "timeout" in error_code:
            return self.tr("Timed out")
        if "authentication" in error_code or "authorization" in error_code or "credential" in error_code:
            return self.tr("Login refused")
        if error_code in {"ptz_camera_lane_limit", "ptz_command_superseded"}:
            return self.tr("Camera busy")
        if "network" in error_code or "unreachable" in error_code:
            return self.tr("Unreachable")
        return self.tr("Failed")

    def _long_error(self, error_code: str) -> str:
        if "timeout" in error_code:
            return self.tr("The camera did not finish moving in time.")
        if "authentication" in error_code or "authorization" in error_code or "credential" in error_code:
            return self.tr("The camera refused the PTZ login. Check it in the camera settings.")
        if error_code in {"ptz_camera_lane_limit", "ptz_command_superseded"}:
            return self.tr("The camera is busy with another command.")
        if "network" in error_code or "unreachable" in error_code:
            return self.tr("The camera did not answer.")
        if error_code == "ptz_preset_slots_full":
            return self.tr("This camera has no free preset slots.")
        return self.tr("The camera could not complete the command.")

    def _remember_speed(self, value: int) -> None:
        if self._camera_id:
            self._speeds[self._camera_id] = int(value)

    # ── keyboard (forwarded by the window) ─────────────────────────────────

    def handle_key_press(self, key: int, auto_repeat: bool) -> bool:
        motion = _MOTION_KEYS.get(key)
        if motion is not None:
            if not auto_repeat or self._moving is None:
                self.start_motion(*motion)
            return True
        if key == Qt.Key.Key_Space.value:
            self.stop_motion()
            return True
        if Qt.Key.Key_1.value <= key <= Qt.Key.Key_9.value:
            if not auto_repeat:
                self.recall_preset_number(key - Qt.Key.Key_0.value)
            return True
        return False

    def handle_key_release(self, key: int, auto_repeat: bool) -> bool:
        if key in _MOTION_KEYS:
            if not auto_repeat:
                self.stop_motion()
            return True
        return False

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop_motion()
        super().hideEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate()
            self.refresh()
        super().changeEvent(event)

    def apply_theme(self) -> None:
        for button in self._pad_buttons.values():
            icon = button.property("icon_svg")
            if isinstance(icon, str) and icon:
                button.setIcon(make_icon(icon, 16, PALETTE.text_secondary))
        self.setStyleSheet(
            f"""
            QWidget#ScenePtzPanel {{ background:{PALETTE.surface}; border-left:1px solid {PALETTE.border}; }}
            QWidget#ScenePtzPanel[live="true"] {{ border-left:2px solid {qss_rgba(PALETTE.danger, 0.70)}; }}
            QLabel#ScenePtzSection {{ color:{PALETTE.text_primary}; font-size:12px; font-weight:600; }}
            QLabel#ScenePtzMuted, QLabel#ScenePtzAlsoIn {{ color:{PALETTE.text_muted}; font-size:11px; }}
            QLabel#ScenePtzLive {{ color:{PALETTE.danger}; font-size:11px; font-weight:600; }}
            QLabel#ScenePtzStatus {{ color:{PALETTE.text_secondary}; font-size:11px; }}
            QLabel#ScenePtzStatus[error="true"] {{ color:{PALETTE.danger}; }}
            QPushButton {{ border:1px solid {PALETTE.border}; border-radius:8px; background:{PALETTE.surface_card}; color:{PALETTE.text_secondary}; padding:4px 10px; font-weight:550; }}
            QPushButton:hover {{ background:{PALETTE.surface_hover}; border-color:{PALETTE.border_strong}; color:{PALETTE.text_primary}; }}
            QPushButton:pressed {{ background:{PALETTE.surface_alt}; }}
            QPushButton:disabled {{ color:{PALETTE.text_faint}; border-color:{PALETTE.border_muted}; }}
            QPushButton[pad="true"] {{ padding:0; font-size:16px; }}
            QPushButton#ScenePtzCamera:checked {{ background:{qss_rgba(PALETTE.accent, 0.14)}; border-color:{qss_rgba(PALETTE.accent, 0.50)}; color:{PALETTE.accent_text}; }}
            QPushButton#ScenePtzPreset {{ text-align:left; padding:0; }}
            QPushButton#ScenePtzPreset[feedback="moving"] {{ border-color:{qss_rgba(PALETTE.accent, 0.60)}; }}
            QPushButton#ScenePtzPreset[feedback="done"] {{ border-color:{qss_rgba(PALETTE.accent, 0.60)}; }}
            QPushButton#ScenePtzPreset[feedback="error"] {{ border-color:{qss_rgba(PALETTE.danger, 0.70)}; }}
            QLabel#ScenePtzPresetName {{ color:{PALETTE.text_secondary}; background:transparent; }}
            QLabel#ScenePtzPresetFeedback {{ color:{PALETTE.accent_text}; background:transparent; font-size:11px; }}
            QPushButton#ScenePtzPreset[feedback="error"] QLabel#ScenePtzPresetFeedback {{ color:{PALETTE.danger}; }}
            QScrollArea#ScenePtzPresets {{ background:transparent; border:none; }}
            """
        )


def _svg(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("PTZ panel icon must be SVG text")
    return value
