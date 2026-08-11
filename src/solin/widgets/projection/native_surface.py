from __future__ import annotations

import ctypes
import logging
import sys
import weakref

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from solin.styles.theme import PALETTE


_LOGGER = logging.getLogger(__name__)
_GWL_EXSTYLE = -20
_LWA_ALPHA = 0x00000002
_INPUT_PLANE_ALPHA = 1
_WS_EX_LAYERED = 0x00080000


class _NativeVideoInputOverlay(QWidget):
    """App-process input plane placed above a cross-process video presenter."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._target_ref: weakref.ReferenceType[QWidget] | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if sys.platform == "win32":
            # Qt's per-pixel translucent backing store uses
            # UpdateLayeredWindowIndirect, which cannot coexist with the
            # global alpha needed to keep a fully transparent HWND in Windows
            # hit testing.  This input-only child never paints; Win32 applies
            # its near-zero global alpha instead.
            self.setUpdatesEnabled(False)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setStyleSheet("background: transparent;")
        self.hide()

    def set_target(self, target: QWidget | None) -> None:
        self._target_ref = weakref.ref(target) if target is not None else None
        self.setVisible(target is not None)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # QShowEvent is delivered before Qt has committed the child HWND.  Run
        # after the current event so winId() resolves to the final live handle.
        QTimer.singleShot(0, self._enable_native_hit_testing)

    def _enable_native_hit_testing(self) -> None:
        if sys.platform != "win32" or not self.isVisible():
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_ssize_t,
        ]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetLayeredWindowAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_ubyte,
            ctypes.c_uint32,
        ]
        user32.SetLayeredWindowAttributes.restype = ctypes.c_int
        handle = ctypes.c_void_p(int(self.winId()))
        ctypes.set_last_error(0)
        extended_style = user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)
        if extended_style == 0 and ctypes.get_last_error() != 0:
            self._log_native_input_error()
            return
        if not extended_style & _WS_EX_LAYERED:
            user32.SetWindowLongPtrW(
                handle,
                _GWL_EXSTYLE,
                extended_style | _WS_EX_LAYERED,
            )
            updated_style = user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)
            if not updated_style & _WS_EX_LAYERED:
                self._log_native_input_error()
                return
        if not user32.SetLayeredWindowAttributes(
            handle,
            0,
            _INPUT_PLANE_ALPHA,
            _LWA_ALPHA,
        ):
            self._log_native_input_error()

    @staticmethod
    def _log_native_input_error() -> None:
        error_code = ctypes.get_last_error()
        _LOGGER.warning(
            "Could not enable native hit testing for the video input plane "
            "(Win32 error %d)",
            error_code,
        )

    def _forward(self, event: QMouseEvent) -> None:
        target = self._target_ref() if self._target_ref is not None else None
        if target is None:
            event.ignore()
            return
        global_position = event.globalPosition()
        target_position = QPointF(
            target.mapFromGlobal(global_position.toPoint())
        )
        forwarded = QMouseEvent(
            event.type(),
            target_position,
            global_position,
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        QApplication.sendEvent(target, forwarded)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._forward(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._forward(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._forward(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._forward(event)


class NativeVideoSurface(QWidget):
    """Stable native host for a platform video presenter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(f"background: {PALETTE.bg0};")
        self._input_overlay: _NativeVideoInputOverlay | None = None
        self.hide()

    @property
    def native_handle(self) -> int:
        return int(self.winId())

    @property
    def input_overlay(self) -> QWidget | None:
        return self._input_overlay

    def set_input_target(self, target: QWidget | None) -> None:
        if self._input_overlay is None and target is not None:
            self._input_overlay = _NativeVideoInputOverlay(self)
        if self._input_overlay is None:
            return
        self._input_overlay.setGeometry(self.rect())
        self._input_overlay.set_target(target)
        if target is not None:
            self._input_overlay.raise_()

    def set_interaction_cursor(self, cursor: Qt.CursorShape) -> None:
        self.setCursor(cursor)
        if self._input_overlay is not None:
            self._input_overlay.setCursor(cursor)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._input_overlay is not None:
            self._input_overlay.setGeometry(self.rect())
            self._input_overlay.raise_()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._input_overlay is not None:
            self._input_overlay.setGeometry(self.rect())
            self._input_overlay.raise_()
