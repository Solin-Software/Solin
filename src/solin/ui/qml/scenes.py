"""Qt Quick host for the Scenes editor."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QUrl
from PySide6.QtGui import (
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QHideEvent,
    QImage,
    QShowEvent,
)
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication, QFileDialog, QVBoxLayout, QWidget

from solin.controllers.program_recording_controller import ProgramRecordingController
from solin.core.foundation.exception_logging import log_ignored_exception
from solin.styles import icons
from solin.styles.theme import PALETTE
from solin.ui.helpers import QmlPointerCursorState
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.scenes_bridge import ScenesBridge
from solin.ui.qml.svg_icons import SvgIconProvider


class ScenePreviewImageProvider(QQuickImageProvider):
    """Thread-safe fallback preview provider for non-DXGI Qt Quick backends."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._lock = RLock()
        self._image = QImage()
        self._generation = 0

    def publish(self, image: QImage) -> str:
        with self._lock:
            self._image = image
            self._generation += 1
            generation = self._generation
        return f"image://scenepreview/frame/{generation}"

    def clear(self) -> str:
        with self._lock:
            self._image = QImage()
            self._generation += 1
            generation = self._generation
        return f"image://scenepreview/empty/{generation}"

    def requestImage(self, id_str: str, size: QSize, requested_size: QSize):  # noqa: N802
        del id_str, size, requested_size
        with self._lock:
            return self._image


_NATIVE_EDITOR_PREVIEW_ENV = "SOLIN_NATIVE_EDITOR_PREVIEW"


def native_editor_preview_enabled() -> bool:
    """Whether the experimental direct-GPU editor preview is active.

    Opt-in via ``SOLIN_NATIVE_EDITOR_PREVIEW=1`` and only where a native window
    handle can be shared cross-process with the sidecar: X11/XWayland (xcb) or
    Windows. Wayland/macOS keep the CPU-readback preview.
    """
    flag = os.environ.get(_NATIVE_EDITOR_PREVIEW_ENV, "").strip().lower()
    if flag not in ("1", "true", "on", "yes"):
        return False
    return QGuiApplication.platformName() in ("xcb", "windows")


class ScenesEditorWidget(QWidget):
    """Own the QML lifecycle while the domain and renderer remain outside QML."""

    def __init__(
        self,
        controller: Any,
        *,
        recording: ProgramRecordingController | None = None,
        credentials: Any | None = None,
        notifications: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cleaned_up = False
        self._pointer_override_owner = ""
        self._preview_provider = ScenePreviewImageProvider()
        self.bridge = ScenesBridge(
            controller,
            preview_store=self._preview_provider,
            recording=recording,
            credentials=credentials,
            notifications=notifications,
            recording_directory_picker=self._pick_recording_directory,
            recording_directory_opener=self._open_recording_directory,
            parent=self,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._qml = QQuickWidget(self)
        self._qml.installEventFilter(self)
        icon_sources = {
            "camera": icons.ICON_CAMERA,
            "video": icons.ICON_VIDEO,
            "image": icons.ICON_IMAGE,
            "plus": icons.ICON_PLUS,
            "trash": icons.ICON_TRASH,
            "edit": icons.ICON_EDIT,
            "grip": icons.ICON_GRIP,
            "eye": icons.ICON_EYE,
            "eye-off": icons.ICON_EYE_OFF,
            "lock": icons.ICON_LOCK,
            "unlock": icons.ICON_UNLOCK,
            "undo": icons.ICON_UNDO,
            "redo": icons.ICON_REDO,
            "more": icons.ICON_MORE_VERT,
            "panel-left": icons.ICON_PANEL_LEFT,
            "panel-right": icons.ICON_PANEL_RIGHT,
            "screen": icons.ICON_SCREEN,
            "chevron-down": icons.ICON_CHEVRON_DOWN,
            "chevron-up": icons.ICON_CHEVRON_UP,
            "refresh": icons.ICON_REFRESH,
            "copy": icons.ICON_COPY,
            "transition": icons.ICON_REPEAT,
            "check": icons.ICON_CHECK,
            "close": icons.ICON_CLOSE,
            "crosshair": icons.ICON_CROSSHAIR,
            "crop": icons.ICON_CROP,
            "warning": icons.ICON_INFO_CIRCLE,
            "record": icons.ICON_REC_CIRCLE,
            "record-stop": icons.ICON_REC_STOP,
            "folder": icons.ICON_FOLDER,
        }
        self.qml_load_handle = configure_qml_host(
            self._qml,
            type_name="ScenesEditorView",
            clear_color=PALETTE.bg0,
            context_properties={"scenes": self.bridge},
            image_providers={
                "sceneicons": SvgIconProvider(icon_sources, default_icon="plus"),
                "scenepreview": self._preview_provider,
            },
            mouse_tracking=True,
        )
        self._pointer_cursor = QmlPointerCursorState(self._qml)
        self.bridge.pointerCursorEntered.connect(self._pointer_cursor.enter_shaped)
        self.bridge.pointerCursorChanged.connect(self._pointer_cursor.update_shaped)
        self.bridge.pointerCursorExited.connect(self._pointer_cursor.exit_shaped)
        self.bridge.pointerOverrideStarted.connect(self._begin_pointer_override)
        self.bridge.pointerOverrideEnded.connect(self._end_pointer_override)
        layout.addWidget(self._qml)

        # Experimental direct-GPU preview: a native surface pinned over the QML
        # canvas frame, into which the sidecar renders the selected scene directly.
        self._controller = controller
        self._preview_surface: Any | None = None
        self._preview_canvas_rect = QRect()
        self._preview_scene_id = str(getattr(controller, "preview_scene_id", "") or "")
        if native_editor_preview_enabled() and hasattr(controller, "set_editor_preview_target"):
            from solin.widgets.projection.native_surface import NativeVideoSurface

            self._preview_surface = NativeVideoSurface(self)
            self.bridge.canvasRectChanged.connect(self._on_preview_canvas_rect)
            scene_changed = getattr(controller, "preview_scene_changed", None)
            if scene_changed is not None:
                scene_changed.connect(self._on_preview_scene)

    def _pick_recording_directory(self, current: str) -> str:
        return QFileDialog.getExistingDirectory(
            self,
            self.tr("Choose recording folder"),
            current,
        )

    @staticmethod
    def _open_recording_directory(path: str) -> bool:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _begin_pointer_override(self, cursor_source: str, cursor_shape: int) -> None:
        if self._pointer_override_owner and self._pointer_override_owner != cursor_source:
            return
        cursor = QCursor(Qt.CursorShape(cursor_shape))
        if self._pointer_override_owner:
            QApplication.changeOverrideCursor(cursor)
        else:
            QApplication.setOverrideCursor(cursor)
            self._pointer_override_owner = cursor_source

    def _end_pointer_override(self, cursor_source: str) -> None:
        if cursor_source != self._pointer_override_owner:
            return
        self._release_pointer_override()

    def _release_pointer_override(self) -> None:
        if not self._pointer_override_owner:
            return
        self._pointer_override_owner = ""
        QApplication.restoreOverrideCursor()

    # ── direct-GPU editor preview surface ─────────────────────────────────

    def _on_preview_canvas_rect(self, x: float, y: float, width: float, height: float) -> None:
        self._preview_canvas_rect = QRect(round(x), round(y), round(width), round(height))
        self._position_preview_surface()
        self._update_preview_target()

    def _on_preview_scene(self, scene_id: str) -> None:
        self._preview_scene_id = scene_id or ""
        self._update_preview_target()

    def _position_preview_surface(self) -> None:
        surface = self._preview_surface
        if surface is None:
            return
        rect = self._preview_canvas_rect
        if self.isVisible() and rect.width() > 0 and rect.height() > 0:
            surface.setGeometry(rect)
            surface.show()
            surface.raise_()  # above the QML content
        else:
            surface.hide()

    def _update_preview_target(self) -> None:
        surface = self._preview_surface
        if surface is None:
            return
        rect = self._preview_canvas_rect
        ready = (
            self.isVisible() and surface.isVisible()
            and rect.width() > 0 and rect.height() > 0 and bool(self._preview_scene_id)
        )
        if not ready:
            self._controller.set_editor_preview_target(None)
            return
        from solin.core.scenes.engine import BusId, OutputWindowTarget

        origin = surface.mapToGlobal(QPoint(0, 0))
        screen = surface.screen() or QApplication.primaryScreen()
        self._controller.set_editor_preview_target(OutputWindowTarget(
            bus_id=BusId.MEDIA_WINDOWS,
            target_id="editor-preview",
            screen_id=(screen.name() or "primary") if screen is not None else "primary",
            native_handle=surface.native_handle,
            x=origin.x(),
            y=origin.y(),
            width=max(1, rect.width()),
            height=max(1, rect.height()),
            device_pixel_ratio=surface.devicePixelRatioF(),
            scene_id=self._preview_scene_id,
        ))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.bridge.setActive(True)
        self._position_preview_surface()
        self._update_preview_target()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        self._release_pointer_override()
        self.bridge.setActive(False)
        if self._preview_surface is not None:
            self._preview_surface.hide()
            self._controller.set_editor_preview_target(None)  # release the display
        super().hideEvent(event)

    def eventFilter(self, watched, event: QEvent) -> bool:  # noqa: N802
        if watched is getattr(self, "_qml", None) and event.type() == QEvent.Type.Leave:
            self._pointer_cursor.reset()
        return super().eventFilter(watched, event)

    def apply_theme(self) -> None:
        apply_qml_theme(self._qml, clear_color=PALETTE.bg0)

    def retranslateUi(self) -> None:  # noqa: N802
        try:
            self._qml.engine().retranslate()
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not refresh Scenes language")
        self.bridge.refresh_language()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self._preview_surface is not None:
            self._controller.set_editor_preview_target(None)  # release the sidecar display
            self._preview_surface.hide()
            self._preview_surface.deleteLater()
            self._preview_surface = None
        self._release_pointer_override()
        self._pointer_cursor.reset()
        root = self._qml.rootObject()
        if root is not None:
            root.setProperty("lifecycleActive", False)
        self.qml_load_handle.cancel()
        try:
            self._qml.setSource(QUrl())
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not clear Scenes QML source")
        self.bridge.close()


__all__ = ["ScenePreviewImageProvider", "ScenesEditorWidget"]
