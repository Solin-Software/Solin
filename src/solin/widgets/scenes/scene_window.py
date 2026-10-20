"""Floating per-scene windows: a large live view of one scene.

Opened from a scene card in the scenes panel. Each scene gets at most one window;
asking again raises it. A window renders its scene through its own engine feed
(one ``scene-window:<id>`` channel each, next to the cards' atlas), sized to the
window and capped at the output resolution, and it says when the scene is on air.

PTZ controls join the window in a later phase; this module is the shell.
"""

from __future__ import annotations

import logging
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.scene_thumbnail_egress import SceneThumbnailEgressController
from solin.core.scenes.engine import scene_window_channel
from solin.core.scenes.model import BusId
from solin.styles.icons import ICON_CAMERA, ICON_SCREEN, make_icon
from solin.styles.theme import PALETTE, qss_rgba
from solin.widgets.scenes.scene_ptz_panel import Confirm, ScenePtzPanel, confirm_with_dialog


log = logging.getLogger(__name__)

# Resizing is continuous; reallocating the shared block on every pixel would
# churn the engine. Settle first, then size the feed once.
_FEED_RESIZE_DEBOUNCE_MS = 150
_GEOMETRY_SAVE_DEBOUNCE_MS = 400
_DEFAULT_SIZE = QSize(960, 600)
_MINIMUM_SIZE = QSize(360, 260)
_MINIMUM_FEED_HEIGHT = 90


class SceneWindowGeometryStore(Protocol):
    def scene_window_geometry(self, scene_id: str) -> bytes: ...

    def set_scene_window_geometry(self, scene_id: str, geometry: bytes) -> None: ...


def scene_feed_size(
    available: QSize,
    device_pixel_ratio: float,
    output_width: int,
    output_height: int,
) -> tuple[int, int]:
    """The feed size for a preview area: fitted to the output, never larger.

    The engine renders exactly what the window will draw, at device pixels, so a
    small window costs little and a large one stays sharp without exceeding the
    resolution the scene is actually composed at. Even sizes keep scalers happy.
    """
    if output_width <= 0 or output_height <= 0:
        output_width, output_height = 1920, 1080
    aspect = output_width / output_height
    ratio = max(1.0, float(device_pixel_ratio))
    width = max(1.0, available.width() * ratio)
    height = max(1.0, available.height() * ratio)
    if width / height > aspect:
        width = height * aspect
    else:
        height = width / aspect
    scale = min(1.0, output_width / width, output_height / height)
    height = max(float(_MINIMUM_FEED_HEIGHT), height * scale)
    width = height * aspect
    return max(2, int(width) // 2 * 2), max(2, int(height) // 2 * 2)


class _LivePreview(QWidget):
    """Draws the latest frame letterboxed at the output's aspect, or a status line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SceneWindowPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._image: QImage | None = None
        self._status = ""
        self._aspect = 16 / 9

    @property
    def image(self) -> QImage | None:
        return self._image

    @property
    def status(self) -> str:
        return self._status

    def set_image(self, image: QImage | None) -> None:
        self._image = image if image is not None and not image.isNull() else None
        self.update()

    def set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self.update()

    def set_aspect(self, aspect: float) -> None:
        if aspect > 0 and aspect != self._aspect:
            self._aspect = aspect
            self.update()

    def frame_rect(self) -> QRectF:
        """Where the scene sits inside the widget, letterboxed to its aspect."""
        width, height = float(self.width()), float(self.height())
        if width <= 0 or height <= 0:
            return QRectF()
        if width / height > self._aspect:
            frame_width, frame_height = height * self._aspect, height
        else:
            frame_width, frame_height = width, width / self._aspect
        return QRectF(
            (width - frame_width) / 2, (height - frame_height) / 2, frame_width, frame_height
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        frame = self.frame_rect()
        if self._image is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(frame, self._image)
        elif self._status:
            painter.setPen(Qt.GlobalColor.gray)
            painter.drawText(frame, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._status)
        painter.end()


class SceneWindow(QWidget):
    """One scene, large and live, in its own resizable top-level window."""

    closed = Signal(str)  # scene_id

    def __init__(
        self,
        controller: SceneRuntimeController,
        scene_id: str,
        *,
        geometry_store: SceneWindowGeometryStore | None = None,
        ptz_speeds: dict[str, int] | None = None,
        confirm: Confirm = confirm_with_dialog,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("SceneWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(_MINIMUM_SIZE)
        self.scene_id = scene_id
        self._controller = controller
        self._geometry_store = geometry_store
        self._channel_id = scene_window_channel(scene_id)
        self._feed = SceneThumbnailEgressController(channel_id=self._channel_id, parent=self)
        self._feed.thumbnail_ready.connect(self._on_frame)
        # What the engine was actually told; cleared whenever it may have forgotten
        # (not ready yet, restarted), so the next sync sends it again.
        self._feed_sent: tuple | None = None
        self._closing = False
        self._ptz_speeds = ptz_speeds
        self._confirm = confirm
        # Keys belong to the window: its controls take no keyboard focus.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.setInterval(_FEED_RESIZE_DEBOUNCE_MS)
        self._feed_timer.timeout.connect(self._sync_feed)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(_GEOMETRY_SAVE_DEBOUNCE_MS)
        self._geometry_timer.timeout.connect(self._save_geometry)

        self._build_ui()
        self._connections = (
            (controller.document_changed, self._render),
            (controller.desired_scenes_changed, self._render),
            (controller.applied_scenes_changed, self._render),
            (controller.runtime_changed, self._render),
            (controller.engine_ready_changed, self._on_engine_ready_changed),
        )
        for signal, handler in self._connections:
            signal.connect(handler)
        self.apply_theme()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("SceneWindowHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 10, 8)
        header_layout.setSpacing(8)
        self._name = QLabel()
        self._name.setObjectName("SceneWindowName")
        header_layout.addWidget(self._name, 1)
        # Same order as everywhere else for outputs: virtual camera, then projection.
        self._camera_tally = self._output_button(
            "SceneWindowCameraTally", _svg(ICON_CAMERA), BusId.VIRTUAL_CAMERA,
        )
        header_layout.addWidget(self._camera_tally)
        self._projection_tally = self._output_button(
            "SceneWindowProjectionTally", _svg(ICON_SCREEN), BusId.MEDIA_WINDOWS,
        )
        header_layout.addWidget(self._projection_tally)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._preview = _LivePreview()
        body.addWidget(self._preview, 1)
        self._ptz = ScenePtzPanel(
            self._controller, self.scene_id, speeds=self._ptz_speeds, confirm=self._confirm,
        )
        self._ptz.hide()
        body.addWidget(self._ptz)
        root.addLayout(body, 1)

    def _output_button(self, object_name: str, icon: str, bus_id: BusId) -> QPushButton:
        """Shows whether the scene is on an output, and puts it there when clicked."""
        button = QPushButton()
        button.setObjectName(object_name)
        button.setProperty("icon_svg", icon)
        button.setProperty("live", False)
        # The window keeps the keys for PTZ; these are mouse controls.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(13, 13))
        button.clicked.connect(lambda: self._route_to(bus_id))
        return button

    def _route_to(self, bus_id: BusId) -> None:
        """The same routing a scene card's output button does."""
        try:
            self._controller.select_scene(bus_id, self.scene_id)
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not route the scene from its window")
        self._render()

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def preview(self) -> _LivePreview:
        return self._preview

    @property
    def ptz_panel(self) -> ScenePtzPanel:
        return self._ptz

    def _scene(self):
        return next(
            (scene for scene in self._controller.document.scenes if scene.id == self.scene_id),
            None,
        )

    def _output_size(self) -> tuple[int, int]:
        video = self._controller.document.output(BusId.VIRTUAL_CAMERA).video_format
        return int(video.width), int(video.height)

    def _render(self, _value: object = None) -> None:
        if self._closing:
            return
        scene = self._scene()
        if scene is None:
            # The scene was deleted (or its profile switched away): nothing to show.
            self.close()
            return
        self._name.setText(scene.name)
        self.setWindowTitle(self.tr("%1 — Solin scenes").replace("%1", scene.name))
        width, height = self._output_size()
        self._preview.set_aspect(width / height if width > 0 and height > 0 else 16 / 9)
        self._render_tally()
        self._render_status()
        self._ptz.refresh()
        self._ptz.setVisible(bool(self._ptz.camera_ids))
        self._sync_feed()

    def _render_tally(self) -> None:
        controller = self._controller
        on_camera = (
            controller.program_output_enabled
            and controller.desired_scene(BusId.VIRTUAL_CAMERA) == self.scene_id
        )
        on_projection = controller.desired_scene(BusId.MEDIA_WINDOWS) == self.scene_id
        for button, live, live_text, idle_text, hint in (
            (
                self._camera_tally, on_camera, self.tr("On air · virtual camera"),
                self.tr("Virtual camera"), self.tr("Show this scene on the virtual camera"),
            ),
            (
                self._projection_tally, on_projection, self.tr("On air · projection"),
                self.tr("Projection"), self.tr("Show this scene on the projection"),
            ),
        ):
            button.setText(live_text if live else idle_text)
            button.setToolTip(live_text if live else hint)
            button.setAccessibleName(live_text if live else hint)
            if button.property("live") != live or button.icon().isNull():
                button.setProperty("live", live)
                button.setIcon(make_icon(
                    str(button.property("icon_svg")), 13,
                    PALETTE.accent_text if live else PALETTE.text_secondary,
                ))
                button.style().unpolish(button)
                button.style().polish(button)
        # Like the card's button: the virtual camera only matters while it runs.
        self._camera_tally.setVisible(controller.program_output_enabled)

    def _render_status(self) -> None:
        if self._controller.engine_ready:
            status = self.tr("Loading preview…")
        elif self._controller.engine_configured:
            status = self.tr("The scene engine is starting…")
        else:
            status = self.tr("The scene engine is unavailable.")
        self._preview.set_status(status)

    def _on_engine_ready_changed(self, ready: bool) -> None:
        if not ready:
            # A restarted engine starts with no feeds; the frame on screen is stale.
            self._feed_sent = None
            self._preview.set_image(None)
        self._render()

    def _on_frame(self, scene_id: str, image: QImage) -> None:
        if scene_id == self.scene_id and not self._closing:
            self._preview.set_image(image)

    # ── engine feed ─────────────────────────────────────────────────────────

    def _feed_size(self) -> tuple[int, int]:
        width, height = self._output_size()
        return scene_feed_size(self._preview.size(), self.devicePixelRatioF(), width, height)

    def _sync_feed(self) -> None:
        if self._closing or not self.isVisible():
            return
        width, height = self._feed_size()
        self._feed.reconfigure((self.scene_id,), width, height)  # no-op when unchanged
        descriptor = self._feed.descriptor
        if descriptor is None:
            return
        desired = (descriptor, width, height)
        if desired == self._feed_sent:
            return
        if self._controller.set_thumbnail_egress(
            descriptor, (self.scene_id,), width, height, channel_id=self._channel_id,
        ):
            self._feed_sent = desired

    def _stop_feed(self) -> None:
        self._feed_timer.stop()
        if self._feed_sent is not None or self._feed.descriptor is not None:
            self._feed.stop()
            self._controller.set_thumbnail_egress(None, (), 0, 0, channel_id=self._channel_id)
        self._feed_sent = None

    # ── geometry ────────────────────────────────────────────────────────────

    def restore_geometry(self) -> None:
        geometry = b""
        if self._geometry_store is not None:
            try:
                geometry = self._geometry_store.scene_window_geometry(self.scene_id)
            except Exception:  # noqa: BLE001 - a bad saved value must not block the window
                log.debug("Could not read the scene window geometry", exc_info=True)
        if geometry and self.restoreGeometry(geometry):
            return
        self.resize(_DEFAULT_SIZE)
        parent = self.parentWidget()
        anchor = parent.window() if parent is not None else None
        screen = (
            anchor.screen() if anchor is not None else None
        ) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            frame = self.frameGeometry()
            frame.moveCenter(area.center())
            self.move(frame.topLeft())

    def _save_geometry(self) -> None:
        if self._geometry_store is None:
            return
        try:
            self._geometry_store.set_scene_window_geometry(
                self.scene_id, bytes(self.saveGeometry().data())
            )
        except Exception:  # noqa: BLE001 - losing a window position is not worth an error
            log.debug("Could not save the scene window geometry", exc_info=True)

    # ── Qt events ───────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._render()

    def hideEvent(self, event) -> None:  # noqa: N802
        # Minimised or hidden: nobody is looking, so stop paying for renders.
        self._stop_feed()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._feed_timer.start()
        self._geometry_timer.start()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._geometry_timer.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if self._ptz.isVisible() and self._ptz.handle_key_press(
            int(event.key()), event.isAutoRepeat()
        ):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if self._ptz.isVisible() and self._ptz.handle_key_release(
            int(event.key()), event.isAutoRepeat()
        ):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: A002, N802 - Qt override
        # Tab and Shift+Tab switch the PTZ camera; nothing else here takes focus.
        if self._ptz.isVisible():
            self._ptz.cycle_camera(1 if next else -1)
            return True
        return super().focusNextPrevChild(next)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # A key released elsewhere never reaches us: never leave a camera moving.
        self._ptz.stop_motion()
        super().focusOutEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._closing:
            self._closing = True
            self._geometry_timer.stop()
            self._save_geometry()
            self._stop_feed()
            self._ptz.stop_motion()
            self._ptz.cancel_captures()
            self._feed.close()
            for signal, handler in self._connections:
                try:
                    signal.disconnect(handler)
                except (RuntimeError, TypeError):
                    log.debug("Scene window signal was already disconnected", exc_info=True)
            self.closed.emit(self.scene_id)
        super().closeEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange and not self._closing:
            self._render()
        super().changeEvent(event)

    def apply_theme(self) -> None:
        self._ptz.apply_theme()
        for button in (self._camera_tally, self._projection_tally):
            button.setIcon(make_icon(
                str(button.property("icon_svg")), 13,
                PALETTE.accent_text if button.property("live") else PALETTE.text_secondary,
            ))
        self.setStyleSheet(
            f"""
            QWidget#SceneWindow {{ background:{PALETTE.surface}; }}
            QWidget#SceneWindowHeader {{ background:{PALETTE.surface}; border-bottom:1px solid {PALETTE.border}; }}
            QLabel {{ background:transparent; }}
            QLabel#SceneWindowName {{ color:{PALETTE.text_primary}; font-size:14px; font-weight:600; background:transparent; }}
            QPushButton#SceneWindowCameraTally, QPushButton#SceneWindowProjectionTally {{
                color:{PALETTE.text_secondary}; background:{PALETTE.surface_card};
                border:1px solid {PALETTE.border}; border-radius:7px;
                padding:3px 10px; font-size:11px; font-weight:600;
            }}
            QPushButton#SceneWindowCameraTally:hover, QPushButton#SceneWindowProjectionTally:hover {{
                background:{PALETTE.surface_hover}; border-color:{PALETTE.border_strong}; color:{PALETTE.text_primary};
            }}
            QPushButton#SceneWindowCameraTally[live="true"], QPushButton#SceneWindowProjectionTally[live="true"] {{
                color:{PALETTE.accent_text}; background:{qss_rgba(PALETTE.accent, 0.14)};
                border-color:{qss_rgba(PALETTE.accent, 0.50)};
            }}
            """
        )


class SceneWindowManager(QObject):
    """Keeps at most one window per scene and remembers where each one was."""

    def __init__(
        self,
        controller: SceneRuntimeController,
        *,
        parent_window: QWidget | None = None,
        geometry_store: SceneWindowGeometryStore | None = None,
        confirm: Confirm = confirm_with_dialog,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._confirm = confirm
        # Speeds are per camera, shared by every window showing it, for the session.
        self._ptz_speeds: dict[str, int] = {}
        self._parent_window = parent_window
        self._geometry_store = geometry_store
        self._windows: dict[str, SceneWindow] = {}
        # Qt deletes child windows with their parent without closing them, which
        # would strand each window's shared-memory block. Close them first.
        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.close_all)

    @property
    def windows(self) -> dict[str, SceneWindow]:
        return dict(self._windows)

    def show_scene(self, scene_id: str) -> SceneWindow | None:
        if not any(scene.id == scene_id for scene in self._controller.document.scenes):
            return None
        window = self._windows.get(scene_id)
        if window is None:
            window = SceneWindow(
                self._controller,
                scene_id,
                geometry_store=self._geometry_store,
                ptz_speeds=self._ptz_speeds,
                confirm=self._confirm,
                parent=self._parent_window,
            )
            window.closed.connect(self._forget)
            self._windows[scene_id] = window
            window.restore_geometry()
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
        return window

    def close_all(self) -> None:
        for window in tuple(self._windows.values()):
            window.close()

    def apply_theme(self) -> None:
        for window in self._windows.values():
            window.apply_theme()

    def _forget(self, scene_id: str) -> None:
        self._windows.pop(scene_id, None)


def _svg(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Scene window icon must be SVG text")
    return value
