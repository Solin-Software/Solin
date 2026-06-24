"""
titlebar.py — Aplica cor personalizada à barra de título nativa.

Suporte por plataforma
──────────────────────
Windows 11+ (build ≥ 22000)
    DwmSetWindowAttribute(DWMWA_CAPTION_COLOR) → cor exata em BGR.

Windows 10 (build ≥ 18985, < 22000)
    DWMWA_USE_IMMERSIVE_DARK_MODE → força barra escura.
    Cor personalizada não suportada pela API neste build.

macOS
    No-op. A ponte Qt/PySide → NSWindow via ctypes/ObjC é frágil em builds
    Nuitka standalone e pode causar crash nativo antes de qualquer exceção
    Python ser capturada.

Linux / outros
    No-op silencioso — a barra de título é desenhada pelo window manager
    e não expõe API pública para personalização por aplicação.

Uso
───
    from solin.ui.titlebar import apply_titlebar_color
    apply_titlebar_color(window)   # após window.show()
"""

from __future__ import annotations

import sys
import logging

from solin.styles.theme import PALETTE

log = logging.getLogger(__name__)

# Cor de texto da barra: escuro → texto branco, claro → texto preto
_DARK_THRESHOLD = 128  # luminância percebida abaixo disto → texto branco


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _perceived_luminance(r: int, g: int, b: int) -> float:
    """Luminância percebida (0–255). Fórmula BT.601."""
    return 0.299 * r + 0.587 * g + 0.114 * b


# ── Windows ───────────────────────────────────────────────────────────────────

def _apply_windows(hwnd: int, hex_color: str) -> bool:
    """
    Tenta aplicar cor de barra de título no Windows.
    Retorna True se conseguiu (qualquer nível de suporte).
    """
    try:
        import ctypes
    except ImportError:
        return False

    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    # ── Detecta build do Windows ──────────────────────────────────────────
    try:
        ver = sys.getwindowsversion()           # type: ignore[attr-defined]
        build = ver.build
    except AttributeError:
        build = 0

    # DWMWA_CAPTION_COLOR (35) — Windows 11 Build 22000+
    DWMWA_CAPTION_COLOR       = 35
    # DWMWA_USE_IMMERSIVE_DARK_MODE — build ≥ 18985 usa attr 20, mais antigo 19
    DWMWA_IMMERSIVE_DARK_MODE = 20 if build >= 18985 else 19
    # DWMWA_TEXT_COLOR (36) — Windows 11 Build 22000+
    DWMWA_TEXT_COLOR          = 36

    applied = False

    if build >= 22000:
        # Windows 11: cor exata da barra de título
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

        # Força texto branco se fundo escuro
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

    # Windows 10+ com dark mode: pelo menos torna a barra escura
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

def apply_titlebar_color(window, hex_color: str = PALETTE.titlebar) -> bool:
    """
    Aplica `hex_color` à barra de título nativa da `window` (QMainWindow ou similar).

    Deve ser chamado APÓS window.show() para garantir que o HWND/NSWindow
    já foi criado pelo sistema operacional.

    Retorna True se algum nível de personalização foi aplicado.
    """
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
