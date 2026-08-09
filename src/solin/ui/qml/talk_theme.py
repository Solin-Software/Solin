"""QML host and clean-image capture boundary for the talk-theme editor."""

from __future__ import annotations

from enum import Enum
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any, Callable, cast

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QEvent,
    QIODevice,
    QObject,
    QSaveFile,
    QSize,
    Slot,
    QTimer,
    Qt,
    QUrl,
)
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.projection.result import ProjectionResult, ProjectionResultStatus
from solin.core.talk_theme.render_target import ThemeRenderTarget
from solin.core.talk_theme.repository import (
    TalkThemeAssetStore,
    TalkThemeRepository,
)
from solin.core.talk_theme.settings import CustomColorSettings, OutputAspectSettings
from solin.styles import icons
from solin.styles.theme import PALETTE
from solin.ui.helpers import QmlPointerCursorState
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.talk_theme_bridge import TalkThemeBridge
from solin.ui.qml.svg_icons import SvgIconProvider


_CAPTURE_TIMEOUT_MS = 5000


def _capture_request_size(canvas: Any, target: ThemeRenderTarget) -> QSize:
    device_pixel_ratio = 1.0
    try:
        window = canvas.window()
        if window is not None:
            device_pixel_ratio = float(window.devicePixelRatio())
    except Exception:  # noqa: BLE001 - defensive QQuickWindow boundary
        device_pixel_ratio = 1.0
    if not isfinite(device_pixel_ratio) or device_pixel_ratio <= 0:
        device_pixel_ratio = 1.0
    return QSize(
        max(1, round(target.width / device_pixel_ratio)),
        max(1, round(target.height / device_pixel_ratio)),
    )


class _CapturePurpose(Enum):
    PROJECT = "project"
    EXPORT = "export"


class TalkThemeEditorWidget(QWidget):
    """Host the editor and reuse one exact rasterization path for output."""

    def __init__(
        self,
        lang: Any,
        *,
        profile_paths: Any,
        notifications: Any,
        projection_session: Any,
        settings: CustomColorSettings,
        output_settings: OutputAspectSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang
        self._projection_handler: Callable[..., ProjectionResult] | None = None
        self._grab_result: Any = None
        self._capture_token = 0
        self._export_path: Path | None = None
        self._cleaned_up = False
        assets_dir = Path(__file__).resolve().parents[2] / "resources" / "assets"
        self.bridge = TalkThemeBridge(
            repository=TalkThemeRepository(profile_paths.talk_theme_file),
            asset_store=TalkThemeAssetStore(profile_paths.talk_theme_assets_dir),
            builtin_asset_dir=assets_dir,
            notifications=notifications,
            projection_session=projection_session,
            projection_windows=projection_session.all_windows,
            settings=settings,
            output_settings=output_settings,
            parent=self,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._qml = QQuickWidget(self)
        self._qml.installEventFilter(self)
        icon_sources = {
            "export": icons.ICON_EXPORT,
            "plus": icons.ICON_PLUS,
            "trash": icons.ICON_TRASH,
            "grip": icons.ICON_GRIP,
            "eye": icons.ICON_EYE,
            "eye-off": icons.ICON_EYE_OFF,
            "align-left": icons.ICON_ALIGN_LEFT,
            "align-center": icons.ICON_ALIGN_CENTER,
            "align-right": icons.ICON_ALIGN_RIGHT,
            "undo": icons.ICON_UNDO,
            "redo": icons.ICON_REDO,
            "more": icons.ICON_MORE_VERT,
            "panel-left": icons.ICON_PANEL_LEFT,
        }
        self.qml_load_handle = configure_qml_host(
            self._qml,
            type_name="TalkThemeEditorView",
            clear_color=PALETTE.bg0,
            context_properties={"talkTheme": self.bridge},
            image_providers={
                "talkthemeicons": SvgIconProvider(
                    icon_sources,
                    default_icon="plus",
                )
            },
            mouse_tracking=True,
        )
        self._qml_pointer_cursor = QmlPointerCursorState(self._qml)
        self.bridge.projectionRequested.connect(self._request_projection_capture)
        self.bridge.exportRequested.connect(self._request_export_capture)
        self.bridge.pointerEntered.connect(self._begin_pointer)
        self.bridge.pointerExited.connect(self._end_pointer)
        layout.addWidget(self._qml)

    def set_projection_handler(
        self,
        handler: Callable[[str, bytes, dict[str, str]], ProjectionResult],
    ) -> None:
        self._projection_handler = handler

    @Slot()
    def _request_projection_capture(self) -> None:
        if self._projection_handler is None:
            self.bridge.cancel_rendering(self.tr("Projection is not ready yet."))
            return
        self._request_capture(_CapturePurpose.PROJECT)

    @Slot()
    def _request_export_capture(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export image"),
            self.bridge.suggested_export_name(),
            self.tr("PNG image (*.png)"),
        )
        if not filename:
            self.bridge.cancel_export()
            return
        path = Path(filename)
        if path.suffix.casefold() != ".png":
            path = path.with_suffix(".png")
        self._request_capture(_CapturePurpose.EXPORT, export_path=path)

    def _request_capture(
        self,
        purpose: _CapturePurpose,
        *,
        export_path: Path | None = None,
    ) -> None:
        self._commit_pending_edits()
        root = self._qml.rootObject()
        canvas = cast(
            Any,
            root.findChild(QObject, "talkThemeCanvas") if root is not None else None,
        )
        if canvas is None:
            self._capture_failed(
                self.tr("The talk-theme canvas could not be loaded."),
                purpose,
            )
            return
        self._capture_token += 1
        token = self._capture_token
        self._export_path = export_path
        target = self.bridge.render_target()
        canvas.setProperty("captureWidth", target.width)
        canvas.setProperty("captureHeight", target.height)
        canvas.setProperty("finalOutput", True)
        QTimer.singleShot(
            0,
            lambda: self._capture_when_ready(
                canvas,
                target,
                token,
                monotonic(),
                purpose,
            ),
        )

    def _capture_when_ready(
        self,
        canvas: Any,
        target: ThemeRenderTarget,
        token: int,
        started: float,
        purpose: _CapturePurpose,
    ) -> None:
        if token != self._capture_token:
            return
        if not bool(canvas.property("readyForCapture")):
            if (monotonic() - started) * 1000 >= _CAPTURE_TIMEOUT_MS:
                canvas.setProperty("finalOutput", False)
                self._capture_failed(
                    self.tr("The background image took too long to load."),
                    purpose,
                )
                return
            QTimer.singleShot(
                25,
                lambda: self._capture_when_ready(
                    canvas,
                    target,
                    token,
                    started,
                    purpose,
                ),
            )
            return
        try:
            result = canvas.grabToImage(_capture_request_size(canvas, target))
        except Exception:  # noqa: BLE001 - Qt Quick render boundary
            canvas.setProperty("finalOutput", False)
            self._capture_failed(
                self.tr("The talk-theme image could not be prepared."),
                purpose,
            )
            return
        if result is None:
            canvas.setProperty("finalOutput", False)
            self._capture_failed(
                self.tr("The talk-theme image could not be prepared."),
                purpose,
            )
            return
        self._grab_result = result
        result.ready.connect(lambda: self._finish_capture(canvas, target, token, purpose))
        QTimer.singleShot(
            _CAPTURE_TIMEOUT_MS,
            lambda: self._capture_timed_out(canvas, token, purpose),
        )

    def _finish_capture(
        self,
        canvas: Any,
        target: ThemeRenderTarget,
        token: int,
        purpose: _CapturePurpose,
    ) -> None:
        if token != self._capture_token or self._grab_result is None:
            return
        result = self._grab_result
        self._grab_result = None
        canvas.setProperty("finalOutput", False)
        image = result.image()
        target_size = QSize(target.width, target.height)
        if image.isNull():
            self._capture_failed(
                self.tr("The final talk-theme image is incomplete."),
                purpose,
            )
            return
        image.setDevicePixelRatio(1.0)
        if image.size() != target_size:
            image = image.scaled(
                target_size,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        if image.isNull() or image.size() != target_size:
            self._capture_failed(
                self.tr("The final talk-theme image is incomplete."),
                purpose,
            )
            return
        payload = QByteArray()
        buffer = QBuffer(payload)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
            self._capture_failed(
                self.tr("The talk-theme image could not be encoded."),
                purpose,
            )
            return
        png = bytes(payload.data())
        if purpose is _CapturePurpose.EXPORT:
            self._finish_export(png)
            return
        self._finish_projection(png, target)

    def _finish_projection(self, payload: bytes, target: ThemeRenderTarget) -> None:
        if self._projection_handler is None:
            self.bridge.cancel_rendering(self.tr("Projection is not ready yet."))
            return
        try:
            result = self._projection_handler(
                self.bridge.output_title(),
                payload,
                {
                    "generated_kind": "talk_theme",
                    "fingerprint": self.bridge.fingerprint(target),
                },
            )
        except Exception:  # noqa: BLE001 - application callback boundary
            log_ignored_exception(__name__, "Could not project generated talk theme")
            result = ProjectionResult(ProjectionResultStatus.FAILED)
        self.bridge.finish_projection(result)

    def _finish_export(self, payload: bytes) -> None:
        path = self._export_path
        self._export_path = None
        if path is None:
            self.bridge.cancel_export()
            return
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            self.bridge.cancel_rendering(
                self.tr("The image could not be exported."),
                export=True,
            )
            return
        if output.write(payload) != len(payload) or not output.commit():
            output.cancelWriting()
            self.bridge.cancel_rendering(
                self.tr("The image could not be exported."),
                export=True,
            )
            return
        self.bridge.finish_export()

    def _capture_timed_out(
        self,
        canvas: Any,
        token: int,
        purpose: _CapturePurpose,
    ) -> None:
        if token != self._capture_token or self._grab_result is None:
            return
        self._capture_token += 1
        self._grab_result = None
        canvas.setProperty("finalOutput", False)
        self._capture_failed(self.tr("Preparing the image timed out."), purpose)

    def _capture_failed(self, message: str, purpose: _CapturePurpose) -> None:
        self._export_path = None
        self.bridge.cancel_rendering(
            message,
            export=purpose is _CapturePurpose.EXPORT,
        )

    def confirm_close(self) -> bool:
        self._commit_pending_edits()
        if not self.bridge.promptOnUnsavedChanges or not self.bridge.dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Unsaved changes"))
        box.setText(self.tr("Save the changes to this talk theme?"))
        save = box.addButton(QMessageBox.StandardButton.Save)
        discard = box.addButton(QMessageBox.StandardButton.Discard)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is discard:
            return True
        if clicked is not save:
            return False
        if self.bridge.can_update_active_preset():
            return self.bridge.save_current_preset()
        name, accepted = QInputDialog.getText(
            self,
            self.tr("Save as preset"),
            self.tr("Name"),
        )
        return bool(accepted and self.bridge.saveAsPreset(name))

    def _commit_pending_edits(self) -> None:
        root = cast(Any, self._qml.rootObject())
        callback = getattr(root, "commitPendingEdits", None)
        if callable(callback):
            callback()

    def _begin_pointer(self, cursor_shape: int) -> None:
        self._qml_pointer_cursor.enter_shaped("talk-theme-canvas", cursor_shape)

    def _end_pointer(self) -> None:
        self._qml_pointer_cursor.exit_shaped("talk-theme-canvas")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is getattr(self, "_qml", None) and event.type() == QEvent.Type.Leave:
            self._qml_pointer_cursor.reset()
        return super().eventFilter(obj, event)

    def apply_theme(self) -> None:
        apply_qml_theme(self._qml, clear_color=PALETTE.bg0)

    def retranslateUi(self) -> None:
        try:
            self._qml.engine().retranslate()
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not refresh talk theme language")
        self.bridge.refresh_language()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._capture_token += 1
        self._grab_result = None
        self._export_path = None
        self._qml_pointer_cursor.reset()
        root = self._qml.rootObject()
        if root is not None:
            root.setProperty("lifecycleActive", False)
        self.qml_load_handle.cancel()
        try:
            self._qml.setSource(QUrl())
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not clear talk theme QML source")
        self.bridge.close()


__all__ = ["TalkThemeBridge", "TalkThemeEditorWidget"]
