from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuickWidgets import QQuickWidget

from solin.ui.qml.loader import load_qml_type


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
    for name, value in (context_properties or {}).items():
        context.setContextProperty(name, value)

    if resize_to_root:
        widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    load_qml_type(widget, type_name)
