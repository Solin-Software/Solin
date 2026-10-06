from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Slot
from PySide6.QtGui import QColor, QEnterEvent, QMouseEvent, QSurfaceFormat
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQuickWidgets import QQuickWidget

from solin.styles.theme import PALETTE, QML_THEME
from solin.ui.helpers import QmlPointerCursorState
from solin.ui.qml.loader import QmlLoadHandle

_QML_CONTROLS_STYLE = "Basic"


class _EmbeddedPointerEnterFilter(QObject):
    """Translate QWidget window coordinates into the embedded Quick scene."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if not isinstance(event, QEnterEvent):
            return False
        widget = self.parent()
        if (
            isinstance(widget, QQuickWidget)
            and watched is widget
            and event.scenePosition() != event.position()
        ):
            # QQuickWidget maps MouseMove to local scene coordinates, but Qt
            # 6.11.1 forwards Enter with the parent window's scenePosition.
            mapped = QEnterEvent(
                event.position(), event.position(), event.globalPosition(), event.pointingDevice()
            )
            mapped.setTimestamp(event.timestamp())
            QCoreApplication.sendEvent(widget.quickWindow(), mapped)
            event.setAccepted(mapped.isAccepted())
            return True
        return False


class _TextFocusDismissFilter(QObject):
    """Dismiss marked QML text focus when a pointer press lands outside it."""

    def __init__(self, widget: QQuickWidget) -> None:
        super().__init__(widget)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        widget = self.parent()
        if (
            isinstance(widget, QQuickWidget)
            and watched is widget
            and event.type() == QEvent.Type.MouseButtonPress
        ):
            self._dismiss_if_outside(widget, event)
        return False

    @classmethod
    def _dismiss_if_outside(cls, widget: QQuickWidget, event: QMouseEvent) -> None:
        focus_item = widget.quickWindow().activeFocusItem()
        boundary = cls._focus_boundary(focus_item)
        if boundary is None:
            return
        local_point = boundary.mapFromScene(event.position())
        if boundary.contains(local_point):
            return
        root = widget.rootObject()
        if isinstance(root, QQuickItem):
            root.forceActiveFocus(Qt.FocusReason.MouseFocusReason)

    @staticmethod
    def _focus_boundary(item: QQuickItem | None) -> QQuickItem | None:
        current = item
        while isinstance(current, QQuickItem):
            if bool(current.property("dismissTextFocusBoundary")):
                return current
            current = current.parentItem()
        return None


class _PointerCursorBridge(QObject):
    """Apply QML pointer hints to every native window owned by a QQuickWidget."""

    def __init__(self, widget: QQuickWidget) -> None:
        super().__init__(widget)
        self._widget = widget
        self._state = QmlPointerCursorState(widget)
        widget.installEventFilter(self)

    @Slot()
    def enter(self) -> None:
        self._state.enter()

    @Slot()
    def exit(self) -> None:
        self._state.exit()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        widget = getattr(self, "_widget", None)
        state = getattr(self, "_state", None)
        if (
            widget is not None
            and state is not None
            and watched is widget
            and event.type() == QEvent.Type.Leave
        ):
            state.reset()
        return False


def current_qml_theme() -> dict[str, Any]:
    return dict(QML_THEME)


def configure_qml_controls_style() -> None:
    """Use a customisable Qt Quick Controls style for all Solin QML surfaces."""
    os.environ["QT_QUICK_CONTROLS_STYLE"] = _QML_CONTROLS_STYLE
    if QQuickStyle.name().casefold() != _QML_CONTROLS_STYLE.casefold():
        QQuickStyle.setStyle(_QML_CONTROLS_STYLE)


def apply_qml_theme(
    widget: QQuickWidget,
    *,
    clear_color: QColor | str | None = None,
) -> None:
    configure_qml_controls_style()
    widget.rootContext().setContextProperty("appTheme", current_qml_theme())
    if clear_color is not None:
        widget.setClearColor(
            clear_color if isinstance(clear_color, QColor) else QColor(clear_color)
        )
    else:
        widget.setClearColor(QColor(PALETTE.bg0))


def configure_qml_host(
    widget: QQuickWidget,
    *,
    type_name: str,
    clear_color: QColor | str,
    context_properties: Mapping[str, Any] | None = None,
    image_providers: Mapping[str, Any] | None = None,
    mouse_tracking: bool = False,
    accept_drops: bool | None = None,
    resize_to_root: bool = True,
    alpha_buffer_size: int = 8,
    defer_load: bool = False,
    dismiss_text_focus_on_pointer_press: bool = False,
) -> QmlLoadHandle:
    configure_qml_controls_style()
    enter_filter = _EmbeddedPointerEnterFilter(widget)
    widget.installEventFilter(enter_filter)
    cast(Any, widget)._qml_pointer_enter_filter = enter_filter
    surface_format = QSurfaceFormat()
    surface_format.setAlphaBufferSize(alpha_buffer_size)
    widget.setFormat(surface_format)
    widget.setClearColor(clear_color if isinstance(clear_color, QColor) else QColor(clear_color))
    widget.setMouseTracking(mouse_tracking)
    pointer_cursor = None
    if mouse_tracking:
        pointer_cursor = _PointerCursorBridge(widget)
        cast(Any, widget)._qml_pointer_cursor_bridge = pointer_cursor
    if dismiss_text_focus_on_pointer_press:
        focus_filter = _TextFocusDismissFilter(widget)
        widget.installEventFilter(focus_filter)
        cast(Any, widget)._qml_text_focus_dismiss_filter = focus_filter
    if accept_drops is not None:
        widget.setAcceptDrops(accept_drops)

    engine = widget.engine()
    for name, provider in (image_providers or {}).items():
        engine.addImageProvider(name, provider)

    context = widget.rootContext()
    merged_context: dict[str, Any] = {"appTheme": current_qml_theme()}
    if pointer_cursor is not None:
        merged_context["appPointerCursor"] = pointer_cursor
    merged_context.update(context_properties or {})
    for name, value in merged_context.items():
        context.setContextProperty(name, value)

    if resize_to_root:
        widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    handle = QmlLoadHandle(widget, type_name, asynchronous=defer_load)
    if not defer_load:
        handle.start()
    return handle
