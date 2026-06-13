"""
macos_layer.py — Arredonda os cantos de um widget via CALayer no macOS.

Contexto
────────
No macOS um ``QQuickWidget`` com ``WA_AlwaysStackOnTop`` compõe os pixels
transparentes da cena QML como **preto opaco** (diferente do Windows, onde o
DWM compõe com alpha por-pixel). Por isso, no macOS, a toolbar roda em "modo
sólido": a pílula é opaca e preenche o widget inteiro (zero pixels
transparentes → zero preto).

Para recuperar os cantos arredondados sem reintroduzir transparência no nível
do QML, pedimos ao **compositor** (CoreAnimation) para recortar a layer da
NSView raiz num retângulo arredondado: ``cornerRadius`` + ``masksToBounds``.
O recorte é feito em hardware, com antialiasing — liso, sem serrilhado, e os
cantos recortados ficam genuinamente transparentes (não pretos).

Segurança
─────────
- Só roda no macOS (``sys.platform == "darwin"``).
- Toda a ponte ObjC é embrulhada em ``try/except`` e nunca propaga exceção.
  Pior caso = no-op → o widget continua um retângulo de cantos retos (a base
  garantida do modo sólido), nunca um crash nativo. Isso respeita o alerta
  documentado em ``titlebar.py`` para builds Nuitka standalone.
- Em qualquer outra plataforma é um no-op silencioso (nem importa pyobjc).

Uso
───
    from solin.core.ui.macos_layer import apply_corner_radius
    apply_corner_radius(self, 20)   # após o widget ter handle nativo (winId)
"""

from __future__ import annotations

import sys
import logging

from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


# Borda da toolbar (#30363d @ 70%), em componentes RGBA 0–1, desenhada na
# própria CALayer para acompanhar o cornerRadius — o QML não desenha borda no
# modo sólido (uma borda retangular do QML seria recortada nos cantos).
_BORDER_RGBA: tuple[float, float, float, float] = (0.1882, 0.2118, 0.2392, 0.7)
_BORDER_WIDTH: float = 1.0


def apply_corner_radius(
    widget: QWidget,
    radius: float,
    *,
    border_width: float = _BORDER_WIDTH,
    border_rgba: tuple[float, float, float, float] | None = _BORDER_RGBA,
) -> bool:
    """Recorta a CALayer raiz do widget num retângulo arredondado no macOS.

    ``masksToBounds`` propaga o recorte às sub-layers (inclusive a layer Metal
    onde o conteúdo opaco é desenhado), então o recorte arredondado vale para
    tudo que estiver dentro do widget. A borda é desenhada na própria layer
    (``borderWidth``/``borderColor``), de modo que acompanha a curva dos cantos.

    Retorna ``True`` se aplicou, ``False`` caso contrário (inclusive fora do
    macOS). Nunca levanta exceção.
    """
    if sys.platform != "darwin":
        return False

    try:
        import objc  # pyobjc-core, trazido por pyobjc-framework-Quartz
        from ctypes import c_void_p
    except (ImportError, OSError):  # pragma: no cover - só ocorre se pyobjc faltar
        log.debug("pyobjc unavailable; rounded corners on macOS ignored")
        return False

    try:
        handle = int(widget.winId())  # força criação do NSView nativo
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
    """Desenha a borda na CALayer (acompanha o cornerRadius). Isolada e segura:
    se a criação do CGColor falhar, o recorte arredondado é mantido sem borda.
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
