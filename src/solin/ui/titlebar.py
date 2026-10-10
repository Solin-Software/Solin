"""
titlebar.py — Apply a custom native title bar color.

Platform support:
Windows 11+ (build ≥ 22000):
    DwmSetWindowAttribute(DWMWA_CAPTION_COLOR) → exact BGR color.

Windows 10 (build ≥ 18985, < 22000):
    DWMWA_USE_IMMERSIVE_DARK_MODE → force a dark title bar.
    This build's API does not support a custom color.

macOS:
    No-op. The Qt/PySide → NSWindow bridge through ctypes/ObjC is fragile
    in Nuitka standalone builds and may cause a native crash before
    Python can catch an exception.

Linux / others:
    Silent no-op. The window manager draws the title bar and provides
    no public per-application customization API.

Usage:
    from solin.ui.titlebar import apply_titlebar_color
    apply_titlebar_color(window)  # after window.show()
"""

from __future__ import annotations

import sys
import logging

from solin.styles.theme import PALETTE

log = logging.getLogger(__name__)

# Title bar text color: white on dark backgrounds, black on light backgrounds.
_DARK_THRESHOLD = 128  # perceived luminance below this threshold → white text


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _perceived_luminance(r: int, g: int, b: int) -> float:
    """Perceived luminance (0–255), using the BT.601 formula."""
    return 0.299 * r + 0.587 * g + 0.114 * b


# ── Windows ───────────────────────────────────────────────────────────────────

def _apply_windows(hwnd: int, hex_color: str) -> bool:
    """
    Try to apply a title bar color on Windows.
    Return True if any supported customization succeeds.
    """
    try:
        import ctypes
    except ImportError:
        return False

    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    # Detect the Windows build.
    try:
        ver = sys.getwindowsversion()           # type: ignore[attr-defined]
        build = ver.build
    except AttributeError:
        build = 0

    # DWMWA_CAPTION_COLOR (35) — Windows 11 Build 22000+
    DWMWA_CAPTION_COLOR       = 35
    # DWMWA_USE_IMMERSIVE_DARK_MODE — build ≥ 18985 uses attr 20; older builds use 19
    DWMWA_IMMERSIVE_DARK_MODE = 20 if build >= 18985 else 19
    # DWMWA_TEXT_COLOR (36) — Windows 11 Build 22000+
    DWMWA_TEXT_COLOR          = 36

    applied = False

    if build >= 22000:
        # Windows 11: exact title bar color
        r, g, b = _hex_to_rgb(hex_color)
        # COLORREF = 0x00BBGGRR
        colorref = ctypes.c_uint32(b << 16 | g << 8 | r)
        hr = dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_CAPTION_COLOR,
            ctypes.byref(colorref),
            ctypes.sizeof(colorref),
        )
        if hr == 0:
            applied = True
            log.debug("DWM caption color applied: %s (COLORREF=0x%06X)", hex_color, colorref.value)

        # Force white text on a dark background.
        lum = _perceived_luminance(*_hex_to_rgb(hex_color))
        text_color = 0xFFFFFF if lum < _DARK_THRESHOLD else 0x000000
        tc = ctypes.c_uint32(
            ((text_color & 0xFF) << 16) |
            (((text_color >> 8) & 0xFF) << 8) |
            ((text_color >> 16) & 0xFF)
        )
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_TEXT_COLOR, ctypes.byref(tc), ctypes.sizeof(tc)
        )

    # Windows 10+ with dark mode: at least make the title bar dark.
    if build >= 18985:
        dark = ctypes.c_uint32(1)
        hr2 = dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_IMMERSIVE_DARK_MODE,
            ctypes.byref(dark),
            ctypes.sizeof(dark),
        )
        if hr2 == 0 and not applied:
            applied = True
            log.debug("DWM dark mode applied (Win10 fallback)")

    return applied


# ── Public API ────────────────────────────────────────────────────────────────

def apply_titlebar_color(window, hex_color: str | None = None) -> bool:
    """
    Apply `hex_color` to the native title bar of `window` (QMainWindow or similar).

    Call AFTER window.show() to ensure the operating system has created
    the HWND/NSWindow.

    Return True if any customization was applied.
    """
    hex_color = hex_color or PALETTE.titlebar
    if not hex_color:
        return False

    try:
        platform = sys.platform

        if platform == "win32":
            hwnd = int(window.winId())
            return _apply_windows(hwnd, hex_color)

        elif platform == "darwin":
            log.debug("Titlebar color disabled on macOS to avoid native ctypes/ObjC crashes.")
            return False

        else:
            # Linux / outros: no-op silencioso
            log.debug("Titlebar color not supported on %s", platform)
            return False

    except Exception as exc:  # noqa: BLE001 - platform-native titlebar boundary
        log.debug("apply_titlebar_color failed: %s", exc)
        return False
