from __future__ import annotations

import logging
import time

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.program_recording_controller import ProgramRecordingController
from solin.core.scenes.composition import SceneComposition, analyze_scene_compositions
from solin.core.scenes.model import BusId, OutputMode, TransitionKind, TransitionSpec
from solin.core.scenes.recording import ProgramRecordingStatus
from solin.styles.icons import (
    ICON_CLAPPERBOARD,
    ICON_REC_CIRCLE,
    ICON_REC_STOP,
    make_icon,
)
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.scene_engine_status import scene_engine_error_summary
from solin.ui.scene_recording_status import (
    scene_recording_audio_warning,
    scene_recording_error_summary,
)
from solin.widgets.common.button_feedback import ButtonSuccessFlash
from solin.widgets.common.flow_container import FlowContainer


log = logging.getLogger(__name__)


class _SmoothScrollArea(QScrollArea):
    """Per-pixel viewport with accumulated, bounded mouse-wheel easing."""

    _WHEEL_DISTANCE = 54
    _DURATION_MS = 140

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        bar = self.verticalScrollBar()
        bar.setSingleStep(18)
        self._scroll_animation = QPropertyAnimation(bar, b"value", self)
        self._scroll_animation.setDuration(self._DURATION_MS)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        bar = self.verticalScrollBar()
        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            self._scroll_animation.stop()
            bar.setValue(bar.value() - pixel_delta)
            event.accept()
            return
        angle_delta = event.angleDelta().y()
        if not angle_delta or bar.maximum() <= bar.minimum():
            super().wheelEvent(event)
            return
        self._animate_delta(round(-(angle_delta / 120) * self._WHEEL_DISTANCE))
        event.accept()

    def _animate_delta(self, delta: int) -> None:
        bar = self.verticalScrollBar()
        target = bar.value()
        if self._scroll_animation.state() is QAbstractAnimation.State.Running:
            end_value = self._scroll_animation.endValue()
            if isinstance(end_value, int):
                target = end_value
        target = max(bar.minimum(), min(target + delta, bar.maximum()))
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(bar.value())
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.start()


class _SceneChipButton(QPushButton):
    """Stable, compact scene chip with native return behavior."""

    return_requested = Signal(str)
    _MIN_WIDTH = 68
    _MAX_WIDTH = 248

    def __init__(self, scene_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene_id = scene_id
        self._return_enabled = False
        self.setObjectName("SceneControlScene")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(32)

    def update_state(
        self,
        *,
        name: str,
        applied: bool,
        desired: bool,
        is_default: bool,
        is_media: bool,
        is_return: bool,
        return_enabled: bool,
        transition_tooltip: str,
        live_text: str,
        selected_text: str,
        default_text: str,
        media_text: str,
        return_text: str,
        return_hint: str,
    ) -> None:
        roles = []
        if is_default:
            roles.append(default_text)
        if is_media:
            roles.append(media_text)
        self._return_enabled = return_enabled and not is_media
        natural_width = self.fontMetrics().horizontalAdvance(name) + 24
        width = max(self._MIN_WIDTH, min(natural_width, self._MAX_WIDTH))
        self.setFixedWidth(width)
        self.setText(
            self.fontMetrics().elidedText(
                name,
                Qt.TextElideMode.ElideRight,
                width - 22,
            )
        )
        self.setProperty("applied", applied)
        self.setProperty("desired", desired)
        self.setProperty("returnTarget", is_return)
        description = [*roles]
        if applied:
            description.append(live_text)
        elif desired:
            description.append(selected_text)
        if is_return:
            description.append(return_text)
        self.setAccessibleName(name)
        self.setAccessibleDescription(", ".join(description))
        tooltips = [name] if self.text() != name else []
        tooltips.extend(roles)
        tooltips.extend(
            value
            for value in (
                transition_tooltip,
                return_hint if self._return_enabled else "",
            )
            if value
        )
        self.setToolTip("\n".join(tooltips))
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() is Qt.MouseButton.RightButton and self._return_enabled:
            self.return_requested.emit(self.scene_id)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        return_shortcut = event.key() is Qt.Key.Key_Menu or (
            event.key() is Qt.Key.Key_F10
            and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        )
        if return_shortcut and self._return_enabled:
            self.return_requested.emit(self.scene_id)
            event.accept()
            return
        super().keyPressEvent(event)


class SceneControlPopup(QWidget):
    """Fast manual scene control without conflating desired and applied state."""

    _PREFERRED_WIDTH = 420
    _MIN_WIDTH = 340
    _MAX_SCENE_VIEWPORT_HEIGHT = 330
    _MIN_SCENE_VIEWPORT_HEIGHT = 56
    _SCREEN_MARGIN = 8
    _ANCHOR_GAP = 10
    _OPERATION_ERROR_DURATION_MS = 5000

    def __init__(
        self,
        controller: SceneRuntimeController,
        parent: QWidget | None = None,
        *,
        recording: ProgramRecordingController | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._PREFERRED_WIDTH)
        self._controller = controller
        self._recording = recording
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None
        self._operation_error = ""
        self._info_full_text = ""
        self._engine_visual_state = "unavailable"
        self._operation_error_timer = QTimer(self)
        self._operation_error_timer.setSingleShot(True)
        self._operation_error_timer.setInterval(self._OPERATION_ERROR_DURATION_MS)
        self._operation_error_timer.timeout.connect(self._clear_operation_error)
        self._recording_clock = QTimer(self)
        self._recording_clock.setInterval(1000)
        self._recording_clock.timeout.connect(self._render_recording)
        self._success_flash = ButtonSuccessFlash(self)
        self._scene_rows: dict[str, _SceneChipButton] = {}
        self._scene_order: tuple[str, ...] = ()
        self._scene_layout_signature: tuple[
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
        ] | None = None
        self._composition_document_key: tuple[str, int] | None = None
        self._scene_compositions: dict[str, SceneComposition] = {}

        opacity = QGraphicsOpacityEffect(self)
        opacity.setOpacity(1.0)
        self.setGraphicsEffect(opacity)
        self._opacity = opacity
        self._fade = QPropertyAnimation(opacity, b"opacity", self)
        self._fade.setDuration(160)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._build_ui()
        controller.document_changed.connect(self._render)
        controller.desired_scenes_changed.connect(self._render)
        controller.applied_scenes_changed.connect(self._render)
        controller.engine_ready_changed.connect(self._render)
        controller.operational_state_changed.connect(self._render)
        controller.runtime_changed.connect(self._render)
        controller.scene_profiles_changed.connect(self._render)
        if recording is not None:
            recording.state_changed.connect(self._render)
        self.apply_theme()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._card = QFrame()
        self._card.setObjectName("SceneControlCard")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(11)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        header.addWidget(self._icon)
        self._title = QLabel(self.tr("Solin scenes"))
        self._title.setObjectName("SceneControlTitle")
        header.addWidget(self._title)
        header.addStretch()
        self._output = QPushButton()
        self._output.setObjectName("SceneOutputToggle")
        self._output.setCheckable(True)
        self._output.clicked.connect(self._set_output_enabled)
        header.addWidget(self._output)
        layout.addLayout(header)

        self._scroll = _SmoothScrollArea()
        self._scroll.setObjectName("SceneControlScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list = QWidget()
        self._list.setObjectName("SceneControlList")
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(7)
        self._configured_label = QLabel(self.tr("Configured"))
        self._configured_label.setObjectName("SceneControlSectionLabel")
        self._list_layout.addWidget(self._configured_label)
        self._configured_cards = FlowContainer(
            horizontal_spacing=8,
            vertical_spacing=6,
        )
        self._configured_cards.setObjectName("SceneControlCardGrid")
        self._list_layout.addWidget(self._configured_cards)
        self._pip_label = QLabel(self.tr("Camera PiP"))
        self._pip_label.setObjectName("SceneControlSectionLabel")
        self._list_layout.addWidget(self._pip_label)
        self._pip_cards = FlowContainer(
            horizontal_spacing=8,
            vertical_spacing=6,
        )
        self._pip_cards.setObjectName("SceneControlCardGrid")
        self._list_layout.addWidget(self._pip_cards)
        self._other_label = QLabel(self.tr("Other scenes"))
        self._other_label.setObjectName("SceneControlSectionLabel")
        self._list_layout.addWidget(self._other_label)
        self._other_cards = FlowContainer(
            horizontal_spacing=8,
            vertical_spacing=6,
        )
        self._other_cards.setObjectName("SceneControlCardGrid")
        self._list_layout.addWidget(self._other_cards)
        self._scroll.setWidget(self._list)
        self._scroll.setMaximumHeight(self._MAX_SCENE_VIEWPORT_HEIGHT)
        layout.addWidget(self._scroll)

        self._info = QLabel()
        self._info.setObjectName("SceneControlInfo")
        self._info.setFixedHeight(16)
        layout.addWidget(self._info)

        separator = QFrame()
        separator.setObjectName("SceneControlSeparator")
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        self._recording_row = QWidget()
        self._recording_row.setObjectName("SceneControlRecordingRow")
        recording_layout = QHBoxLayout(self._recording_row)
        recording_layout.setContentsMargins(0, 0, 0, 0)
        recording_layout.setSpacing(0)
        self._recording_button = QPushButton()
        self._recording_button.setObjectName("SceneControlRecording")
        self._recording_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recording_button.clicked.connect(self._toggle_recording)
        recording_layout.addWidget(self._recording_button, 1)
        layout.addWidget(self._recording_row)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._automatic = QPushButton(self.tr("Auto-switch media"))
        self._automatic.setCheckable(True)
        self._automatic.setObjectName("SceneControlAutomatic")
        self._automatic.clicked.connect(self._set_automatic)
        footer.addWidget(self._automatic, 1)
        self._media_mirror = QPushButton(self.tr("Media windows"))
        self._media_mirror.setCheckable(True)
        self._media_mirror.setObjectName("SceneControlMediaWindows")
        self._media_mirror.clicked.connect(self._set_media_mirror_enabled)
        self._media_mirror.setToolTip(self.tr("Show in media windows"))
        footer.addWidget(self._media_mirror, 1)
        layout.addLayout(footer)
        root.addWidget(self._card)

    def _toggle_recording(self) -> None:
        if self._recording is None:
            return
        try:
            self._recording.toggle()
            self._set_operation_error("")
        except Exception:  # noqa: BLE001 - recording controller boundary
            log.exception("Could not toggle Program recording")
            self._set_operation_error(self.tr("The recording state could not be changed."))
        self._render()

    def _render_recording(self) -> None:
        if self._recording is None:
            self._recording_clock.stop()
            self._recording_row.hide()
            return
        state = self._recording.state
        status = state.status
        is_recording = status is ProgramRecordingStatus.RECORDING
        is_active = status in {
            ProgramRecordingStatus.STARTING,
            ProgramRecordingStatus.RECORDING,
            ProgramRecordingStatus.STOPPING,
        }
        if is_active and not self._recording_clock.isActive():
            self._recording_clock.start()
        elif not is_active:
            self._recording_clock.stop()
        if is_recording:
            started = state.started_at_monotonic
            elapsed = 0 if started is None else max(0, int(time.monotonic() - started))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            elapsed_text = (
                f"{hours}:{minutes:02d}:{seconds:02d}"
                if hours
                else f"{minutes:02d}:{seconds:02d}"
            )
            text = self.tr("Stop recording · %1").replace("%1", elapsed_text)
            icon = ICON_REC_STOP
            audio_warnings = tuple(
                value
                for value in (
                    scene_recording_audio_warning(
                        state.microphone_warning,
                        "microphone",
                    ),
                    scene_recording_audio_warning(
                        state.system_audio_warning,
                        "system_audio",
                    ),
                )
                if value
            )
            tooltip = "\n".join((self.tr("Stop recording"), *audio_warnings))
        elif status is ProgramRecordingStatus.STARTING:
            text = self.tr("Starting recording…")
            icon = ICON_REC_CIRCLE
            tooltip = text
        elif status is ProgramRecordingStatus.STOPPING:
            text = self.tr("Finishing recording…")
            icon = ICON_REC_STOP
            tooltip = text
        elif status is ProgramRecordingStatus.FAILED:
            text = self.tr("Try recording again")
            icon = ICON_REC_CIRCLE
            tooltip = scene_recording_error_summary(state.error_code)
        else:
            text = self.tr("Start recording")
            icon = ICON_REC_CIRCLE
            tooltip = self.tr("Start recording the live output")
        self._recording_button.setText(text)
        self._recording_button.setIcon(make_icon(_svg(icon), 15, PALETTE.danger))
        self._recording_button.setEnabled(
            status is ProgramRecordingStatus.RECORDING
            or (
                self._controller.engine_ready
                and self._recording.supported
                and status
                in {
                    ProgramRecordingStatus.IDLE,
                    ProgramRecordingStatus.FAILED,
                }
            )
        )
        self._recording_button.setProperty("recording", is_active)
        if not self._recording_button.isEnabled():
            if not self._controller.engine_ready:
                tooltip = self.tr("The scene engine must be ready to record.")
            elif not self._recording.supported:
                tooltip = self.tr("Recording is unavailable in this scene engine.")
        self._recording_button.setToolTip(tooltip)
        self._recording_button.setAccessibleName(text)
        self._recording_button.setAccessibleDescription(tooltip)
        self._recording_row.show()
        self._repolish(self._recording_button)

    def _take(self, scene_id: str) -> None:
        try:
            self._controller.take_program_scene(scene_id)
            self._set_operation_error("")
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not select the requested scene")
            self._set_operation_error(self.tr("The scene could not be selected."))
        self._render()

    def _set_return_scene(self, scene_id: str) -> None:
        succeeded = False
        try:
            self._controller.set_program_return_scene(scene_id)
            self._set_operation_error("")
            succeeded = True
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the Program return scene")
            self._set_operation_error(
                self.tr("The return scene could not be changed."),
            )
        self._render()
        if succeeded:
            self._success_flash.flash(self._scene_rows[scene_id])

    def _set_automatic(self, enabled: bool) -> None:
        try:
            self._controller.set_program_automatic(bool(enabled))
            self._set_operation_error("")
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update automatic scene switching")
            self._set_operation_error(self.tr("Automatic switching could not be updated."))
        self._render()

    def _set_output_enabled(self, checked: bool) -> None:
        try:
            self._controller.set_output_enabled(BusId.VIRTUAL_CAMERA, bool(checked))
            self._set_operation_error("")
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the scene output state")
            self._set_operation_error(self.tr("The output state could not be changed."))
        self._render()

    def _set_media_mirror_enabled(self, checked: bool) -> None:
        try:
            self._controller.set_output_enabled(BusId.MEDIA_WINDOWS, bool(checked))
            self._set_operation_error("")
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the media-window mirror state")
            self._set_operation_error(self.tr("The media-window mirror could not be changed."))
        self._render()

    def _set_operation_error(self, text: str) -> None:
        self._operation_error_timer.stop()
        self._operation_error = text
        if text:
            self._operation_error_timer.start()

    def _clear_operation_error(self) -> None:
        if not self._operation_error:
            return
        self._operation_error = ""
        self._render()

    def _render(self, _value: object = None) -> None:
        self._render_recording()
        runtime = self._controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
        mirror = self._controller.runtime.state.output(BusId.MEDIA_WINDOWS)
        desired = self._controller.desired_scene(BusId.VIRTUAL_CAMERA)
        applied = self._controller.applied_scene(BusId.VIRTUAL_CAMERA)
        engine_state = "unavailable"

        if self._controller.hydration_in_progress:
            engine_text = self.tr("Preparing…")
            engine_state = "preparing"
        elif self._controller.last_engine_error_code:
            error_code = self._controller.last_engine_error_code
            error_summary = scene_engine_error_summary(error_code)
            engine_text = f"{self.tr('Error')}: {error_summary} ({error_code})"
            engine_state = "error"
        elif self._controller.engine_ready:
            engine_text = self.tr("Ready")
            engine_state = "ready"
        elif self._controller.engine_configured:
            engine_text = self.tr("Starting…")
            engine_state = "preparing"
        else:
            engine_text = self.tr("Unavailable")
        self._set_engine_visual(engine_state, engine_text)

        default_scene_id = self._controller.documents.program_default_scene_id
        media_scene_id = self._controller.documents.program_media_scene_id
        return_scene_id = self._controller.program_return_scene_id
        return_override_available = self._controller.program_return_override_available
        self._sync_scene_rows(
            desired=desired,
            applied=applied,
            default_scene_id=default_scene_id,
            media_scene_id=media_scene_id,
            return_scene_id=return_scene_id,
            return_override_available=return_override_available,
        )

        if (
            self._recording is not None
            and self._recording.state.status is ProgramRecordingStatus.FAILED
        ):
            state = self._recording.state
            info_text = scene_recording_error_summary(state.error_code)
        elif engine_state != "ready":
            info_text = engine_text
        elif self._operation_error:
            info_text = self._operation_error
        elif self._controller.program_automation_suspended:
            info_text = self.tr("Auto-switch paused for this media session")
        elif return_override_available and return_scene_id:
            return_name = self._controller.document.scene(return_scene_id).name
            info_text = self.tr("Return: %1 · Right-click to change").replace("%1", return_name)
        else:
            info_text = self.tr("Scene engine ready")
        self._set_info_text(info_text)

        self._automatic.blockSignals(True)
        self._automatic.setChecked(runtime.mode is OutputMode.AUTO)
        self._automatic.setProperty("suspended", self._controller.program_automation_suspended)
        automation_configured = self._controller.documents.program_automation_configured
        self._automatic.setEnabled(automation_configured)
        self._automatic.setToolTip(
            self.tr("Paused for this media session after a manual scene change.")
            if self._controller.program_automation_suspended
            else ""
            if automation_configured
            else self.tr("Choose different default and media scenes first.")
        )
        self._repolish(self._automatic)
        self._automatic.blockSignals(False)
        self._output.blockSignals(True)
        self._output.setChecked(runtime.enabled)
        self._output.setText(self.tr("Virtual camera"))
        self._output.setToolTip(self.tr("Output on") if runtime.enabled else self.tr("Output off"))
        self._output.setAccessibleName(self.tr("Virtual camera"))
        self._output.setAccessibleDescription(self._output.toolTip())
        self._output.blockSignals(False)
        self._media_mirror.blockSignals(True)
        self._media_mirror.setChecked(mirror.enabled)
        self._media_mirror.blockSignals(False)
        self._sync_geometry()

    def _sync_scene_rows(
        self,
        *,
        desired: str,
        applied: str | None,
        default_scene_id: str | None,
        media_scene_id: str | None,
        return_scene_id: str,
        return_override_available: bool,
    ) -> None:
        document = self._controller.document
        scenes = document.scenes
        source_order = tuple(scene.id for scene in scenes)
        configured_ids = {
            scene_id
            for scene_id in (default_scene_id, media_scene_id)
            if scene_id is not None and scene_id in source_order
        }
        configured_order = tuple(
            scene_id for scene_id in source_order if scene_id in configured_ids
        )
        composition_document_key = (document.document_id, document.revision)
        if composition_document_key != self._composition_document_key:
            self._scene_compositions = analyze_scene_compositions(document)
            self._composition_document_key = composition_document_key
        pip_order = tuple(
            scene_id
            for scene_id in source_order
            if scene_id not in configured_ids
            and self._scene_compositions[scene_id].has_camera_over_content
        )
        pip_ids = set(pip_order)
        other_order = tuple(
            scene_id
            for scene_id in source_order
            if scene_id not in configured_ids and scene_id not in pip_ids
        )
        layout_signature = (configured_order, pip_order, other_order)
        if layout_signature != self._scene_layout_signature:
            scroll_value = self._scroll.verticalScrollBar().value()
            self._configured_cards.clear_items(delete=False)
            self._pip_cards.clear_items(delete=False)
            self._other_cards.clear_items(delete=False)
            for removed_id in set(self._scene_rows) - set(source_order):
                self._scene_rows.pop(removed_id).deleteLater()
            for scene in scenes:
                if scene.id in self._scene_rows:
                    continue
                row = _SceneChipButton(scene.id, self._list)
                row.installEventFilter(self)
                row.clicked.connect(lambda _checked=False, scene_id=scene.id: self._take(scene_id))
                row.return_requested.connect(self._set_return_scene)
                self._scene_rows[scene.id] = row

            for scene_id in configured_order:
                self._configured_cards.add_widget(self._scene_rows[scene_id])
            for scene_id in pip_order:
                self._pip_cards.add_widget(self._scene_rows[scene_id])
            for scene_id in other_order:
                self._other_cards.add_widget(self._scene_rows[scene_id])
            has_configured = bool(configured_order)
            has_pip = bool(pip_order)
            has_other = bool(other_order)
            self._configured_label.setVisible(has_configured)
            self._configured_cards.setVisible(has_configured)
            self._pip_label.setVisible(has_pip)
            self._pip_cards.setVisible(has_pip)
            self._other_label.setVisible(has_other)
            self._other_cards.setVisible(has_other)
            self._scene_order = configured_order + pip_order + other_order
            self._scene_layout_signature = layout_signature
            QTimer.singleShot(
                0,
                lambda value=scroll_value: self._scroll.verticalScrollBar().setValue(value),
            )

        for scene in scenes:
            override = self._controller.document.transition_policy.override_for(scene.id)
            transition_tooltip = (
                ""
                if override is None
                else self.tr("Transition override: %1").replace(
                    "%1", self._transition_label(override)
                )
            )
            self._scene_rows[scene.id].update_state(
                name=scene.name,
                applied=scene.id == applied,
                desired=scene.id == desired,
                is_default=scene.id == default_scene_id,
                is_media=scene.id == media_scene_id,
                is_return=return_override_available and scene.id == return_scene_id,
                return_enabled=return_override_available,
                transition_tooltip=transition_tooltip,
                live_text=self.tr("LIVE"),
                selected_text=self.tr("SELECTED"),
                default_text=self.tr("Default scene"),
                media_text=self.tr("Media scene"),
                return_text=self.tr("Return scene"),
                return_hint=self.tr("Right-click to return here when media ends."),
            )

    def _transition_label(self, spec: TransitionSpec) -> str:
        labels = {
            TransitionKind.CUT: self.tr("Cut"),
            TransitionKind.DISSOLVE: self.tr("Dissolve"),
            TransitionKind.FADE_TO_BLACK: self.tr("Fade through black"),
        }
        label = labels[spec.kind]
        return label if spec.kind is TransitionKind.CUT else f"{label} · {spec.duration_ms} ms"

    def _set_engine_visual(self, state: str, description: str) -> None:
        self._engine_visual_state = state
        color = PALETTE.text_faint if state == "ready" else PALETTE.warning
        self._icon.setPixmap(make_icon(_svg(ICON_CLAPPERBOARD), 18, color).pixmap(18, 18))
        self._icon.setToolTip(description)
        self._icon.setAccessibleName(self.tr("Scene engine"))
        self._icon.setAccessibleDescription(description)

    def _set_info_text(self, text: str) -> None:
        self._info_full_text = text
        available_width = max(1, self._info.width())
        visible_text = self._info.fontMetrics().elidedText(
            text,
            Qt.TextElideMode.ElideRight,
            available_width,
        )
        self._info.setText(visible_text)
        self._info.setToolTip(text if visible_text != text else "")
        self._info.setAccessibleName(self.tr("Scene status"))
        self._info.setAccessibleDescription(text)

    def show_above(self, anchor: QWidget) -> None:
        top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        self._anchor_rect = QRect(top_left.x(), top_left.y(), anchor.width(), anchor.height())
        screen = (
            QGuiApplication.screenAt(self._anchor_rect.center()) or QGuiApplication.primaryScreen()
        )
        self._available_rect = screen.availableGeometry() if screen is not None else None
        self._fade.stop()
        self._opacity.setOpacity(0.0)
        self._render()
        self.show()
        QTimer.singleShot(0, self._finish_show)

    def _finish_show(self) -> None:
        if not self.isVisible():
            return
        self._sync_geometry()
        self.raise_()
        self.activateWindow()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _sync_geometry(self) -> None:
        self.ensurePolished()
        available = (
            self._available_rect.adjusted(
                self._SCREEN_MARGIN,
                self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
            )
            if self._available_rect is not None
            else None
        )
        width = self._PREFERRED_WIDTH
        if available is not None:
            width = max(self._MIN_WIDTH, min(width, available.width()))
            width = min(width, available.width())
        self.setFixedWidth(width)

        card_layout = self._card.layout()
        card_margins = card_layout.contentsMargins() if card_layout is not None else None
        content_width = width - (
            card_margins.left() + card_margins.right() if card_margins is not None else 0
        )
        self._info.setFixedWidth(content_width)
        self._set_info_text(self._info_full_text)
        for flow in (self._configured_cards, self._pip_cards, self._other_cards):
            if not flow.isVisible():
                continue
            flow.setFixedSize(
                content_width,
                max(32, flow.heightForWidth(content_width)),
            )
            # Chip widths can change without resizing the container (rename,
            # language change, or document refresh), so resizeEvent alone is
            # not a sufficient layout trigger.
            flow.relayout()
        self._list_layout.activate()
        content_height = max(
            self._MIN_SCENE_VIEWPORT_HEIGHT,
            self._list_layout.sizeHint().height(),
        )
        preferred_viewport_height = min(
            content_height,
            self._MAX_SCENE_VIEWPORT_HEIGHT,
        )
        self._scroll.setFixedHeight(preferred_viewport_height)
        if card_layout is not None:
            card_layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()
        preferred_height = max(1, self.sizeHint().height())
        if self._anchor_rect is None:
            self.setFixedHeight(preferred_height)
            return

        place_above = True
        if available is not None:
            space_above = max(0, self._anchor_rect.top() - self._ANCHOR_GAP - available.top())
            space_below = max(
                0,
                available.bottom() - self._anchor_rect.bottom() - self._ANCHOR_GAP,
            )
            place_above = preferred_height <= space_above or space_above >= space_below
            side_capacity = space_above if place_above else space_below
            if preferred_height > side_capacity:
                chrome_height = preferred_height - preferred_viewport_height
                height_capacity = side_capacity
                if side_capacity < chrome_height + self._MIN_SCENE_VIEWPORT_HEIGHT:
                    # On an exceptionally short work area no side of the anchor
                    # can contain the popup. Preserve the fixed header/status/
                    # footer and let the clamped popup overlap the anchor instead
                    # of letting Qt overlap its own children.
                    height_capacity = available.height()
                viewport_height = max(
                    1,
                    min(preferred_viewport_height, height_capacity - chrome_height),
                )
                self._scroll.setFixedHeight(viewport_height)
                if card_layout is not None:
                    card_layout.activate()
                if root_layout is not None:
                    root_layout.activate()
                preferred_height = max(1, self.sizeHint().height())
            preferred_height = min(preferred_height, available.height())
        self.setFixedHeight(preferred_height)

        x = self._anchor_rect.center().x() - self.width() // 2
        y = (
            self._anchor_rect.top() - self.height() - self._ANCHOR_GAP
            if place_above
            else self._anchor_rect.bottom() + self._ANCHOR_GAP + 1
        )
        if available is not None:
            x = max(available.left(), min(x, available.right() - self.width() + 1))
            y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def apply_theme(self) -> None:
        self._set_engine_visual(self._engine_visual_state, self._icon.toolTip())
        self.setStyleSheet(
            f"""
            QFrame#SceneControlCard {{ background:{PALETTE.surface}; border:1px solid {PALETTE.border}; border-radius:14px; }}
            QWidget {{ background:transparent; color:{PALETTE.text_secondary}; }}
            QLabel#SceneControlTitle {{ color:{PALETTE.text_faint}; font-size:13px; font-weight:650; }}
            QLabel#SceneControlSectionLabel {{ color:{PALETTE.text_muted}; font-size:10px; font-weight:600; padding:1px 4px 0 4px; }}
            QWidget#SceneControlCardGrid {{ background:transparent; }}
            QPushButton {{ min-height:32px; padding:0 11px; border:1px solid {PALETTE.border}; border-radius:8px; background:{PALETTE.surface_card}; color:{PALETTE.text_secondary}; font-weight:550; }}
            QPushButton:hover {{ background:{PALETTE.surface_hover}; border-color:{PALETTE.border_strong}; color:{PALETTE.text_primary}; }}
            QPushButton:focus {{ border-color:{PALETTE.accent_alt}; }}
            QPushButton:pressed {{ background:{PALETTE.surface_alt}; }}
            QPushButton#SceneOutputToggle {{ min-width:84px; }}
            QPushButton#SceneOutputToggle:checked, QPushButton#SceneControlAutomatic:checked, QPushButton#SceneControlMediaWindows:checked {{ background:{qss_rgba(PALETTE.accent, 0.10)}; border-color:{qss_rgba(PALETTE.accent, 0.42)}; color:{PALETTE.accent_text}; }}
            QPushButton#SceneControlRecording {{ min-width:180px; color:{PALETTE.danger}; border-color:{qss_rgba(PALETTE.danger, 0.38)}; }}
            QPushButton#SceneControlRecording:hover {{ background:{qss_rgba(PALETTE.danger, 0.10)}; border-color:{qss_rgba(PALETTE.danger, 0.62)}; color:{PALETTE.danger}; }}
            QPushButton#SceneControlRecording[recording="true"] {{ background:{qss_rgba(PALETTE.danger, 0.10)}; border-color:{qss_rgba(PALETTE.danger, 0.58)}; color:{PALETTE.danger}; }}
            QPushButton#SceneControlAutomatic[suspended="true"] {{ background:{PALETTE.warning_surface}; border-color:{PALETTE.warning_border}; color:{PALETTE.warning_text}; }}
            QPushButton#SceneControlScene {{ min-height:32px; padding:0 10px; background:{qss_rgba(PALETTE.surface, 0.60)}; border:1px solid {qss_rgba(PALETTE.border, 0.60)}; border-radius:8px; color:{PALETTE.text_muted}; font-size:12px; font-weight:400; text-align:center; }}
            QPushButton#SceneControlScene:hover {{ background:{PALETTE.surface_hover}; border-color:{qss_rgba(PALETTE.accent, 0.35)}; color:{PALETTE.text_secondary}; }}
            QPushButton#SceneControlScene:focus {{ border-color:{PALETTE.accent_alt}; color:{PALETTE.text_primary}; }}
            QPushButton#SceneControlScene[desired="true"] {{ background:{qss_rgba(PALETTE.accent, 0.08)}; border-color:{qss_rgba(PALETTE.accent, 0.30)}; color:{PALETTE.accent_text}; }}
            QPushButton#SceneControlScene[applied="true"] {{ background:{qss_rgba(PALETTE.accent, 0.12)}; border-color:{qss_rgba(PALETTE.accent, 0.40)}; color:{PALETTE.accent_hover}; font-weight:600; }}
            QPushButton:disabled {{ color:{PALETTE.text_faint}; border-color:{PALETTE.border_muted}; }}
            QLabel#SceneControlInfo {{ color:{PALETTE.text_muted}; font-size:10px; padding:0 2px; }}
            QFrame#SceneControlSeparator {{ background:{PALETTE.border_muted}; border:none; }}
            QScrollArea#SceneControlScroll {{ border:none; background:transparent; }}
            QWidget#SceneControlList {{ background:transparent; }}
            QScrollBar:vertical {{ width:5px; margin-left:2px; background:transparent; }}
            QScrollBar::handle:vertical {{ background:{PALETTE.border}; border-radius:2px; min-height:24px; }}
            QScrollBar::handle:vertical:hover {{ background:{PALETTE.text_dim}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
            """
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if isinstance(watched, _SceneChipButton):
            if event.type() == QEvent.Type.FocusIn:
                self._scroll.ensureWidgetVisible(watched, 0, 8)
            elif event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                key = event.key()
                target_scene_id: str | None = None
                if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                    target_scene_id = self._vertical_scene_neighbor(
                        watched,
                        direction=-1 if key == Qt.Key.Key_Up else 1,
                    )
                elif watched.scene_id in self._scene_order:
                    current = self._scene_order.index(watched.scene_id)
                    if key == Qt.Key.Key_Left:
                        target = max(0, current - 1)
                    elif key == Qt.Key.Key_Right:
                        target = min(current + 1, len(self._scene_order) - 1)
                    elif key == Qt.Key.Key_Home:
                        target = 0
                    elif key == Qt.Key.Key_End:
                        target = len(self._scene_order) - 1
                    else:
                        target = current
                    if target != current or key in (
                        Qt.Key.Key_Home,
                        Qt.Key.Key_End,
                    ):
                        target_scene_id = self._scene_order[target]
                if target_scene_id is not None:
                    self._scene_rows[target_scene_id].setFocus(Qt.FocusReason.ShortcutFocusReason)
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _vertical_scene_neighbor(
        self,
        current: _SceneChipButton,
        *,
        direction: int,
    ) -> str | None:
        origin = current.mapTo(self._list, current.rect().center())
        candidates: list[tuple[int, int, int, str]] = []
        for order, scene_id in enumerate(self._scene_order):
            widget = self._scene_rows[scene_id]
            if widget is current or not widget.isVisible() or not widget.isEnabled():
                continue
            center = widget.mapTo(self._list, widget.rect().center())
            vertical_delta = center.y() - origin.y()
            if vertical_delta * direction <= 0:
                continue
            candidates.append(
                (
                    abs(vertical_delta),
                    abs(center.x() - origin.x()),
                    order,
                    scene_id,
                )
            )
        return min(candidates)[3] if candidates else None

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._set_operation_error("")
            self._title.setText(self.tr("Solin scenes"))
            self._configured_label.setText(self.tr("Configured"))
            self._pip_label.setText(self.tr("Camera PiP"))
            self._other_label.setText(self.tr("Other scenes"))
            self._automatic.setText(self.tr("Auto-switch media"))
            self._media_mirror.setText(self.tr("Media windows"))
            self._media_mirror.setToolTip(self.tr("Show in media windows"))
            self._render()
        super().changeEvent(event)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _svg(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Scene icon must be SVG text")
    return value
