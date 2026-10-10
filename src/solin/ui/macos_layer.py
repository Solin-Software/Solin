"""
macos_layer.py — Round widget corners through CALayer on macOS.

Context:
On macOS, ``QQuickWidget`` with ``WA_AlwaysStackOnTop`` composites transparent
QML scene pixels as opaque black. Windows DWM uses per-pixel alpha. Therefore
the macOS toolbar uses solid mode: the opaque pill fills the entire widget
(no transparent pixels, no black).

To restore rounded corners without QML-level transparency, CoreAnimation
clips the root NSView layer to a rounded rectangle using ``cornerRadius``
and ``masksToBounds``. Hardware clipping is antialiased, with smooth edges
and transparent rather than black clipped corners.

Safety:
- Runs only on macOS with the Qt Cocoa plugin active. Headless backends such
  as ``offscreen`` have no valid ``NSView`` behind ``winId()``.
- Python/ObjC exceptions become no-ops. Check the backend before interpreting
  ``winId()`` as an ``NSView``: an invalid native pointer may abort the process
  before Python can turn the failure into an exception.
- Silently does nothing on other platforms, without importing pyobjc.

Usage:
    from solin.ui.macos_layer import apply_corner_radius
    apply_corner_radius(self, 20)  # after the widget has a native handle (winId)
"""

from __future__ import annotations

import sys
import logging

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


# Toolbar border (#30363d @ 70%), in 0–1 RGBA components, drawn on
# the CALayer itself to follow cornerRadius; QML draws no border in
# solid mode (a rectangular QML border would be clipped at the corners).
_BORDER_RGBA: tuple[float, float, float, float] = (0.1882, 0.2118, 0.2392, 0.7)
_BORDER_WIDTH: float = 1.0


def apply_corner_radius(
    widget: QWidget,
    radius: float,
    *,
    border_width: float = _BORDER_WIDTH,
    border_rgba: tuple[float, float, float, float] | None = _BORDER_RGBA,
) -> bool:
    """
    Clip the widget's root CALayer to a rounded rectangle on macOS.

    ``masksToBounds`` propagates clipping to sublayers, including the Metal
    layer rendering opaque content, so all widget content follows the rounded
    clip. Draw the border on the layer itself (``borderWidth``/``borderColor``)
    so it follows the corner curves.

    Return ``True`` if applied, otherwise ``False`` (including outside macOS).
    Never raise an exception.
    """
    if sys.platform != "darwin":
        return False
    application = QGuiApplication.instance()
    if application is None or application.platformName() != "cocoa":
        return False

    try:
        import objc  # pyobjc-core, included by pyobjc-framework-Quartz
        from ctypes import c_void_p
    except (ImportError, OSError):  # pragma: no cover - only occurs when pyobjc is missing
        log.debug("pyobjc unavailable; rounded corners on macOS ignored")
        return False

    try:
        handle = int(widget.winId())  # force creation of the native NSView
    except Exception:  # noqa: BLE001 - Qt/Cocoa bridge boundary
        log.debug("winId() unavailable while rounding corners", exc_info=True)
        return False

    if not handle:
        return False

    try:
        view = objc.objc_object(c_void_p=c_void_p(handle))
        view.setWantsLayer_(True)
        layer = view.layer()
        if layer is None:
            return False
        layer.setCornerRadius_(float(radius))
        layer.setMasksToBounds_(True)
        _apply_layer_border(layer, border_width, border_rgba)
        return True
    except Exception:  # noqa: BLE001 - Objective-C runtime boundary
        log.debug("Failed to apply cornerRadius on CALayer", exc_info=True)
        return False


def _apply_layer_border(layer, border_width: float, border_rgba) -> None:
    """
    Draw the border on CALayer, following cornerRadius.
    Isolated and safe: if CGColor creation fails, retain rounded clipping without a border.
    """
    if border_rgba is None or border_width <= 0:
        try:
            layer.setBorderWidth_(0.0)
        except Exception:  # noqa: BLE001 - Objective-C runtime boundary
            log.debug("Failed to remove CALayer border", exc_info=True)
        return
    try:
        from Quartz import CGColorCreateGenericRGB

        r, g, b, a = border_rgba
        layer.setBorderColor_(CGColorCreateGenericRGB(r, g, b, a))
        layer.setBorderWidth_(float(border_width))
    except Exception:  # noqa: BLE001 - Quartz/Objective-C runtime boundary
        log.debug("Failed to draw CALayer border; rounded corners remain applied", exc_info=True)
