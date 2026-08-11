from __future__ import annotations

import logging

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.scenes.model import BusId, OutputMode
from solin.styles.icons import ICON_CLAPPERBOARD, make_icon
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.scene_engine_status import scene_engine_error_summary


log = logging.getLogger(__name__)


class SceneControlPopup(QWidget):
    """Fast manual scene control without conflating desired and applied state."""

    _WIDTH = 420
    _SCREEN_MARGIN = 8
    _ANCHOR_GAP = 10

    def __init__(
        self,
        controller: SceneRuntimeController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._WIDTH)
        self._controller = controller
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None
        self._feedback_text = ""

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
        self.apply_theme()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._card = QFrame()
        self._card.setObjectName("SceneControlCard")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(18, 17, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        header.addWidget(self._icon)
        title_column = QVBoxLayout()
        title_column.setSpacing(0)
        self._title = QLabel(self.tr("Solin scenes"))
        self._title.setObjectName("SceneControlTitle")
        self._engine_status = QLabel()
        self._engine_status.setObjectName("SceneControlStatus")
        title_column.addWidget(self._title)
        title_column.addWidget(self._engine_status)
        header.addLayout(title_column, 1)
        layout.addLayout(header)

        output_row = QHBoxLayout()
        output_copy = QVBoxLayout()
        output_copy.setSpacing(0)
        self._output_title = QLabel(self.tr("Virtual camera"))
        self._output_title.setObjectName("SceneOutputTitle")
        self._program_status = QLabel()
        self._program_status.setObjectName("SceneOutputStatus")
        output_copy.addWidget(self._output_title)
        output_copy.addWidget(self._program_status)
        output_row.addLayout(output_copy, 1)
        self._output = QPushButton()
        self._output.setCheckable(True)
        self._output.clicked.connect(self._set_output_enabled)
        output_row.addWidget(self._output)
        layout.addLayout(output_row)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("SceneControlScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._scroll.setWidget(self._list)
        self._scroll.setMaximumHeight(330)
        layout.addWidget(self._scroll)

        self._feedback = QLabel()
        self._feedback.setObjectName("SceneControlFeedback")
        self._feedback.setWordWrap(True)
        self._feedback.hide()
        layout.addWidget(self._feedback)

        footer = QHBoxLayout()
        self._automatic = QPushButton(self.tr("Auto-switch media"))
        self._automatic.setCheckable(True)
        self._automatic.setObjectName("SceneControlAutomatic")
        self._automatic.clicked.connect(self._set_automatic)
        footer.addWidget(self._automatic, 1)
        self._media_mirror = QPushButton(self.tr("Show in media windows"))
        self._media_mirror.setCheckable(True)
        self._media_mirror.clicked.connect(self._set_media_mirror_enabled)
        footer.addWidget(self._media_mirror)
        layout.addLayout(footer)
        root.addWidget(self._card)

    def _take(self, scene_id: str) -> None:
        try:
            self._controller.take_program_scene(scene_id)
            self._feedback_text = ""
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not select the requested scene")
            self._feedback_text = self.tr("The scene could not be selected.")
        self._render()

    def _set_automatic(self, enabled: bool) -> None:
        try:
            self._controller.set_program_automatic(bool(enabled))
            self._feedback_text = ""
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not resume automatic scene switching")
            self._feedback_text = self.tr("Automatic switching could not be resumed.")
        self._render()

    def _set_output_enabled(self, checked: bool) -> None:
        try:
            self._controller.set_output_enabled(BusId.VIRTUAL_CAMERA, bool(checked))
            self._feedback_text = ""
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the scene output state")
            self._feedback_text = self.tr("The output state could not be changed.")
        self._render()

    def _set_media_mirror_enabled(self, checked: bool) -> None:
        try:
            self._controller.set_output_enabled(BusId.MEDIA_WINDOWS, bool(checked))
            self._feedback_text = ""
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the media-window mirror state")
            self._feedback_text = self.tr("The media-window mirror could not be changed.")
        self._render()

    def _render(self, _value: object = None) -> None:
        runtime = self._controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
        mirror = self._controller.runtime.state.output(BusId.MEDIA_WINDOWS)
        desired = self._controller.desired_scene(BusId.VIRTUAL_CAMERA)
        applied = self._controller.applied_scene(BusId.VIRTUAL_CAMERA)
        self._engine_status.setToolTip("")

        if self._controller.hydration_in_progress:
            self._engine_status.setText(self.tr("Preparing scenes…"))
            self._engine_status.setProperty("ready", False)
        elif self._controller.last_engine_error_code:
            error_code = self._controller.last_engine_error_code
            error_summary = scene_engine_error_summary(error_code)
            self._engine_status.setText(
                self.tr("Scene error: %1").replace("%1", error_summary)
            )
            self._engine_status.setToolTip(f"{error_summary} ({error_code})")
            self._engine_status.setProperty("ready", False)
        elif self._controller.engine_ready:
            self._engine_status.setText(self.tr("Scene engine ready"))
            self._engine_status.setProperty("ready", True)
        elif self._controller.engine_configured:
            self._engine_status.setText(self.tr("Scene engine is starting"))
            self._engine_status.setProperty("ready", False)
        else:
            self._engine_status.setText(self.tr("Scene engine unavailable"))
            self._engine_status.setProperty("ready", False)
        self._repolish(self._engine_status)

        desired_name = self._controller.document.scene(desired).name
        if applied is not None:
            applied_name = self._controller.document.scene(applied).name
            self._program_status.setText(
                self.tr("Live · %1").replace("%1", applied_name)
                if applied == desired
                else self.tr("Live · %1 · switching to %2")
                .replace("%1", applied_name)
                .replace("%2", desired_name)
            )
        elif self._controller.hydration_in_progress:
            self._program_status.setText(self.tr("Preparing scenes…"))
        elif self._controller.last_engine_error_code:
            error_summary = scene_engine_error_summary(
                self._controller.last_engine_error_code
            )
            self._program_status.setText(
                self.tr("Error · %1").replace("%1", error_summary)
            )
        else:
            self._program_status.setText(self.tr("Not live"))

        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        default_scene_id = self._controller.documents.program_default_scene_id
        media_scene_id = self._controller.documents.program_media_scene_id
        for scene in self._controller.document.scenes:
            button = QPushButton()
            button.setObjectName("SceneControlScene")
            button.setProperty("desired", scene.id == desired)
            button.setProperty("applied", scene.id == applied)
            markers = []
            if scene.id == applied:
                markers.append(self.tr("LIVE"))
            elif scene.id == desired:
                markers.append(self.tr("SELECTED"))
            if scene.id == default_scene_id:
                markers.append(self.tr("DEFAULT"))
            if scene.id == media_scene_id:
                markers.append(self.tr("MEDIA"))
            suffix = f"  ·  {' · '.join(markers)}" if markers else ""
            button.setText(f"{scene.name}{suffix}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, scene_id=scene.id: self._take(scene_id))
            self._list_layout.addWidget(button)
        self._list_layout.addStretch()

        self._automatic.blockSignals(True)
        self._automatic.setChecked(runtime.mode is OutputMode.AUTO)
        automation_configured = self._controller.documents.program_automation_configured
        self._automatic.setEnabled(automation_configured)
        self._automatic.setToolTip(
            ""
            if automation_configured
            else self.tr("Choose different default and media scenes first.")
        )
        self._automatic.blockSignals(False)
        self._output.blockSignals(True)
        self._output.setChecked(runtime.enabled)
        self._output.setText(
            self.tr("Output on") if runtime.enabled else self.tr("Output off")
        )
        self._output.blockSignals(False)
        self._media_mirror.blockSignals(True)
        self._media_mirror.setChecked(mirror.enabled)
        self._media_mirror.blockSignals(False)
        self._feedback.setText(self._feedback_text)
        self._feedback.setVisible(bool(self._feedback_text))
        self._sync_geometry()

    def show_above(self, anchor: QWidget) -> None:
        top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        self._anchor_rect = QRect(top_left.x(), top_left.y(), anchor.width(), anchor.height())
        screen = QGuiApplication.screenAt(self._anchor_rect.center()) or QGuiApplication.primaryScreen()
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
        card_layout = self._card.layout()
        if card_layout is not None:
            card_layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()
        self.setFixedSize(self._WIDTH, max(1, self.sizeHint().height()))
        if self._anchor_rect is None:
            return
        x = self._anchor_rect.center().x() - self.width() // 2
        y = self._anchor_rect.top() - self.height() - self._ANCHOR_GAP
        if self._available_rect is not None:
            available = self._available_rect.adjusted(
                self._SCREEN_MARGIN,
                self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
            )
            x = max(available.left(), min(x, available.right() - self.width() + 1))
            if y < available.top():
                y = self._anchor_rect.bottom() + self._ANCHOR_GAP + 1
            y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def apply_theme(self) -> None:
        self._icon.setPixmap(
            make_icon(_svg(ICON_CLAPPERBOARD), 20, PALETTE.accent).pixmap(20, 20)
        )
        self.setStyleSheet(
            f"""
            QFrame#SceneControlCard {{ background:{PALETTE.surface}; border:1px solid {PALETTE.border}; border-radius:16px; }}
            QWidget {{ background:transparent; color:{PALETTE.text_secondary}; }}
            QLabel#SceneControlTitle {{ color:{PALETTE.text_primary}; font-size:14px; font-weight:700; }}
            QLabel#SceneControlStatus {{ color:{PALETTE.warning_text}; font-size:10px; }}
            QLabel#SceneControlStatus[ready="true"] {{ color:{PALETTE.success}; }}
            QPushButton {{ min-height:32px; padding:0 11px; border:1px solid {PALETTE.border}; border-radius:8px; background:{PALETTE.surface_card}; color:{PALETTE.text_secondary}; font-weight:600; }}
            QPushButton:hover {{ border-color:{PALETTE.accent_alt}; color:{PALETTE.text_primary}; }}
            QPushButton:checked {{ background:{PALETTE.accent_muted}; border-color:{PALETTE.accent}; color:{PALETTE.accent_text_hover}; }}
            QPushButton#SceneControlScene {{ text-align:left; min-height:38px; }}
            QPushButton#SceneControlScene[desired="true"] {{ border-color:{PALETTE.accent}; background:{qss_rgba(PALETTE.accent, 0.12)}; }}
            QPushButton#SceneControlScene[applied="true"] {{ border-color:{PALETTE.success}; }}
            QPushButton:disabled {{ color:{PALETTE.text_faint}; border-color:{PALETTE.border_muted}; }}
            QLabel#SceneControlFeedback {{ color:{PALETTE.danger_text}; font-size:10px; }}
            QScrollArea#SceneControlScroll {{ border:none; background:transparent; }}
            QScrollBar:vertical {{ width:4px; background:transparent; }}
            QScrollBar::handle:vertical {{ background:{PALETTE.border}; border-radius:2px; min-height:20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            """
        )

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._title.setText(self.tr("Solin scenes"))
            self._output_title.setText(self.tr("Virtual camera"))
            self._automatic.setText(self.tr("Auto-switch media"))
            self._media_mirror.setText(self.tr("Show in media windows"))
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
