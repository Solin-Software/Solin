from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQuickWidgets import QQuickWidget

from solin.styles.theme import PALETTE, QML_THEME
from solin.ui.qml.loader import load_qml_type

_QML_CONTROLS_STYLE = "Basic"


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
    elif widget.clearColor().isValid():
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
) -> None:
    configure_qml_controls_style()
    surface_format = QSurfaceFormat()
    surface_format.setAlphaBufferSize(alpha_buffer_size)
    widget.setFormat(surface_format)
    widget.setClearColor(clear_color if isinstance(clear_color, QColor) else QColor(clear_color))
    widget.setMouseTracking(mouse_tracking)
    if accept_drops is not None:
        widget.setAcceptDrops(accept_drops)

    engine = widget.engine()
    for name, provider in (image_providers or {}).items():
        engine.addImageProvider(name, provider)

    context = widget.rootContext()
    merged_context: dict[str, Any] = {"appTheme": current_qml_theme()}
    merged_context.update(context_properties or {})
    for name, value in merged_context.items():
        context.setContextProperty(name, value)

    if resize_to_root:
        widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    load_qml_type(widget, type_name)
