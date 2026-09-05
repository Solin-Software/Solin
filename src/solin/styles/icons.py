"""
Ícones SVG centralizados — usados em toda a aplicação.

  Widgets Qt : make_icon(ICON_X, size, color)  →  QIcon
  Overlay JS : importar JS_SVG_IMAGE, JS_SVG_VIDEO
"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont, QFontDatabase
from PySide6.QtSvg import QSvgRenderer

from functools import lru_cache
import sys

from solin.styles.theme import PALETTE

# Declared without eagerly constructing the SVG returned by __getattr__.
ICON_CURSOR_HOVER: str

# ── Configuracao de bandeiras ─────────────────────────────────────────────────
#
# USE_EMOJI_FLAGS = True   → renderiza bandeiras como emoji Unicode (🇧🇷 🇺🇸 etc.)
#                            Funciona em macOS e Linux (fontes de emoji colorido
#                            suportam indicadores regionais nesses SOs).
#
# USE_EMOJI_FLAGS = False  → usa os SVGs hardcoded (comportamento original).
#                            Unico modo que funciona corretamente no Windows,
#                            pois o Windows nao suporta emoji de bandeiras
#                            regionais (Segoe UI Emoji nao inclui esses glifos).
#
# Deteccao automatica por plataforma. Para forcar um valor, substitua por:
#   USE_EMOJI_FLAGS: bool = True   ou   USE_EMOJI_FLAGS: bool = False
#
USE_EMOJI_FLAGS: bool = sys.platform != "win32"

# Família de fonte emoji preferida (primeira disponível no sistema é usada).
# Você pode ajustar esta lista se quiser priorizar outra fonte.
_EMOJI_FONT_PREFERENCE = [
    "Noto Color Emoji",  # Linux (Google Noto)
    "Apple Color Emoji",  # macOS
    "Segoe UI Emoji",  # Windows 10/11
    "Twemoji Mozilla",  # Firefox / alguns Linux
    "EmojiOne Mozilla",  # Fallback
]


def resolve_emoji_font(size: int) -> QFont:
    """
    Retorna um QFont configurado para a melhor fonte de emoji colorido disponivel.
    Funcao publica para uso nos widgets (QLabel.setFont).
    """
    available = set(QFontDatabase.families())
    for family in _EMOJI_FONT_PREFERENCE:
        if family in available:
            f = QFont(family)
            f.setPixelSize(size)
            return f
    f = QFont()
    f.setPixelSize(size)
    return f


def make_emoji_flag_icon(emoji: str, size: int = 24) -> QIcon:
    """
    Renderiza um emoji de bandeira como QIcon via QTextDocument.
    Usa o motor de texto do Qt (mais confiavel que QPainter.drawText
    para color emoji em todas as plataformas).
    Retorna QIcon vazio se o emoji nao puder ser renderizado.
    """
    if not emoji:
        return QIcon()

    from PySide6.QtGui import QTextDocument
    from PySide6.QtCore import QSizeF

    font = resolve_emoji_font(size)
    canvas = size + 8  # margem para o glifo nao ser cortado

    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setHtml(
        f'<p style="margin:0;padding:0;'
        f"font-family:'{font.family()}';"
        f'font-size:{size}px;">{emoji}</p>'
    )
    doc.setPageSize(QSizeF(canvas, canvas))

    pix = QPixmap(canvas, canvas)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    doc.drawContents(p)
    p.end()

    return QIcon(pix)


@lru_cache(maxsize=128)
def _render_flag_icon(svg_str: str, size: int) -> QIcon:
    """Render one immutable multicolor flag icon on the GUI thread."""
    data = QByteArray(svg_str.encode("utf-8"))
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        return QIcon()
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    renderer.render(p)
    p.end()
    return QIcon(pix)


def make_flag_icon(svg_str: str, size: int = 24) -> QIcon:
    return QIcon(_render_flag_icon(svg_str, size))


@lru_cache(maxsize=512)
def _render_icon(svg_str: str, size: int, color: str) -> QIcon:
    """Render one immutable themed icon on the GUI thread."""
    svg = svg_str.replace("currentColor", color)
    data = QByteArray(svg.encode("utf-8"))
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        return QIcon()
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def make_icon(svg_str: str, size: int = 18, color: str | None = None) -> QIcon:
    """Renderiza SVG string como QIcon, substituindo 'currentColor' por *color*."""
    color = str(color or PALETTE.text_secondary)
    return QIcon(_render_icon(svg_str, size, color))


# ── Qt widget icons (usa currentColor para fácil coloração) ───────────────────

_SVG_CACHE: dict[str, str] = {}
_ICON_NAMES = frozenset(
    (
        "ICON_PLAY",
        "ICON_PAUSE",
        "ICON_PLAY_PAUSE",
        "ICON_VOLUME_HIGH",
        "ICON_VOLUME_LOW",
        "ICON_VOLUME_MUTE",
        "ICON_MUSIC",
        "ICON_SONG_ANNOUNCEMENT",
        "ICON_VIDEO",
        "ICON_CAMERA",
        "ICON_IMAGE",
        "ICON_CLOSE",
        "ICON_MORE_VERT",
        "ICON_MENU",
        "ICON_ARROW_LEFT",
        "ICON_IMPORT",
        "ICON_EXPORT",
        "ICON_PLUS",
        "ICON_TRASH",
        "ICON_EDIT",
        "ICON_GRIP",
        "ICON_EYE",
        "ICON_EYE_OFF",
        "ICON_LOCK",
        "ICON_UNLOCK",
        "ICON_ALIGN_LEFT",
        "ICON_ALIGN_CENTER",
        "ICON_ALIGN_RIGHT",
        "ICON_UNDO",
        "ICON_REDO",
        "ICON_REPEAT",
        "ICON_REFRESH",
        "ICON_SHUFFLE",
        "ICON_SKIP_NEXT",
        "ICON_SKIP_PREV",
        "ICON_PANEL_LEFT",
        "ICON_PANEL_RIGHT",
        "ICON_PANEL_BOTTOM",
        "ICON_SCREEN",
        "ICON_PLAY_ALL",
        "ICON_PLAY_SHUFFLE",
        "ICON_CHEVRON_DOWN",
        "ICON_CHEVRON_UP",
        "ICON_NAV_LIBRARY",
        "ICON_NAV_BROWSER",
        "ICON_NAV_TIMER",
        "ICON_NAV_SETTINGS",
        "ICON_MONITOR",
        "ICON_TV",
        "ICON_SET_AS_IDLE",
        "ICON_CAST",
        "ICON_NAV_THEME",
        "ICON_NAV_PLAYLIST",
        "ICON_ADD_TO_PLAYLIST",
        "ICON_SEND_TO_PLAYLIST",
        "ICON_SAVE_PLAYLIST",
        "ICON_CROP",
        "ICON_MEDIA_TRIM",
        "ICON_MEDIA_LANGUAGE",
        "ICON_CLOUD_DOWNLOAD",
        "ICON_MANUAL_DOWNLOAD",
        "ICON_CLOUD_DONE",
        "ICON_AUDIO_COVER",
        "ICON_AUTO_DOWNLOAD",
        "JS_SVG_IMAGE",
        "JS_SVG_VIDEO",
        "ICON_NAV_WIFI",
        "ICON_NAV_MEETINGS",
        "ICON_HOME",
        "ICON_CHEVRON_LEFT",
        "ICON_CHEVRON_RIGHT",
        "ICON_CALENDAR",
        "ICON_FLAG_PT_BR",
        "ICON_FLAG_EN_US",
        "ICON_FLAG_ES",
        "ICON_FLAG_FR_FR",
        "ICON_FLAG_IT_IT",
        "ICON_FLAG_ZH_CN",
        "ICON_FLAG_JA_JP",
        "ICON_OBS",
        "ICON_REC_CIRCLE",
        "ICON_REC_STOP",
        "ICON_FOLDER",
        "ICON_FOLDER_LINK",
        "ICON_ZOOM",
        "ICON_PEOPLE",
        "ICON_INFO_CIRCLE",
        "ICON_SPEAKER_PHONE",
        "ICON_OVERLAY_CLOSE",
        "ICON_BOOK",
        "ICON_PLUG",
        "ICON_CLAPPERBOARD",
        "ICON_PACKAGE",
        "ICON_KEYBOARD",
        "ICON_CROSSHAIR",
        "ICON_COPY",
        "ICON_CHECK",
        "ICON_SHIELD",
        "ICON_REMOTE_CONTROL",
        "ICON_SHARE_SCREEN",
        "ICON_ASPECT_MATCH",
        "ICON_BOUNDS",
        "ICON_CURSOR_HOVER",
        "ICON_SECTION",
        "ICON_MARKER",
        "ICON_PALETTE",
        "ICON_FULLSCREEN",
        "ICON_FULLSCREEN_EXIT",
    )
)
_FLAG_ICON_NAMES: dict[str, str] = {
    "pt_BR": "ICON_FLAG_PT_BR",
    "en": "ICON_FLAG_EN_US",
    "es": "ICON_FLAG_ES",
    "fr_FR": "ICON_FLAG_FR_FR",
    "it_IT": "ICON_FLAG_IT_IT",
    "zh_CN": "ICON_FLAG_ZH_CN",
    "ja_JP": "ICON_FLAG_JA_JP",
}


def _build_icon_svg(name: str) -> str:
    """Build one SVG string on first access."""
    if name == "ICON_CURSOR_HOVER":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="1.7" '
            'stroke-linecap="round" stroke-linejoin="round">'
            '<path d="m5 5 5.5 14 2.5-6 6-2.5L5 5Z"/>'
            '<path d="M14 3v2M19 5l-1.5 1.5M21 10h-2"/>'
            '</svg>'
        )
    elif name == "ICON_PLAY":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<polygon points="5,3 19,12 5,21" fill="currentColor"/>'
            "</svg>"
        )
    elif name == "ICON_PAUSE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<rect x="5" y="3" width="4" height="18" rx="1" fill="currentColor"/>'
            '<rect x="15" y="3" width="4" height="18" rx="1" fill="currentColor"/>'
            "</svg>"
        )
    elif name == "ICON_PLAY_PAUSE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="4,4 13,12 4,20" fill="currentColor" stroke="none"/>'
            '<line x1="17" y1="5" x2="17" y2="19"/>'
            '<line x1="21" y1="5" x2="21" y2="19"/>'
            "</svg>"
        )
    elif name == "ICON_VOLUME_HIGH":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="11,5 6,9 2,9 2,15 6,15 11,19"/>'
            '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
            '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>'
            "</svg>"
        )
    elif name == "ICON_VOLUME_LOW":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="11,5 6,9 2,9 2,15 6,15 11,19"/>'
            '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
            "</svg>"
        )
    elif name == "ICON_VOLUME_MUTE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="11,5 6,9 2,9 2,15 6,15 11,19"/>'
            '<line x1="23" y1="9" x2="17" y2="15"/>'
            '<line x1="17" y1="9" x2="23" y2="15"/>'
            "</svg>"
        )
    elif name == "ICON_MUSIC":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M9 18V5l12-2v13"/>'
            '<circle cx="6" cy="18" r="3"/>'
            '<circle cx="18" cy="16" r="3"/>'
            "</svg>"
        )
    elif name == "ICON_SONG_ANNOUNCEMENT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
            '<rect x="3.2" y="3.2" width="17.6" height="17.6" rx="4.2"'
            ' stroke="currentColor" stroke-width="1.9"/>'
            '<path d="M7.2 8.5H16.8" stroke="currentColor" stroke-width="1.9"'
            ' stroke-linecap="round"/>'
            '<path d="M8.6 12.2H15.4" stroke="currentColor" stroke-width="1.9"'
            ' stroke-linecap="round"/>'
            "</svg>"
        )
    elif name == "ICON_VIDEO":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="23,7 16,12 23,17 23,7"/>'
            '<rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>'
            "</svg>"
        )
    elif name == "ICON_CAMERA":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M14.5 4.5 16.2 7H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h3.8l1.7-2.5z"/>'
            '<circle cx="12" cy="13" r="3.5"/>'
            "</svg>"
        )
    elif name == "ICON_IMAGE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="18" rx="2"/>'
            '<circle cx="8.5" cy="8.5" r="1.5"/>'
            '<path d="m21 15-5-5L5 21"/>'
            "</svg>"
        )
    elif name == "ICON_CLOSE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round">'
            '<line x1="18" y1="6" x2="6" y2="18"/>'
            '<line x1="6" y1="6" x2="18" y2="18"/>'
            "</svg>"
        )
    elif name == "ICON_MORE_VERT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<circle cx="12" cy="5" r="2" fill="currentColor"/>'
            '<circle cx="12" cy="12" r="2" fill="currentColor"/>'
            '<circle cx="12" cy="19" r="2" fill="currentColor"/>'
            "</svg>"
        )
    elif name == "ICON_MENU":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<line x1="4" y1="6" x2="20" y2="6"/>'
            '<line x1="4" y1="12" x2="20" y2="12"/>'
            '<line x1="4" y1="18" x2="20" y2="18"/>'
            "</svg>"
        )
    elif name == "ICON_ARROW_LEFT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M19 12H5"/>'
            '<polyline points="12,5 5,12 12,19"/>'
            "</svg>"
        )
    elif name == "ICON_IMPORT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
            '<path d="M17 8l-5 5-5-5"/>'
            '<line x1="12" y1="13" x2="12" y2="3"/>'
            "</svg>"
        )
    elif name == "ICON_EXPORT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
            '<path d="M17 8l-5-5-5 5"/>'
            '<line x1="12" y1="3" x2="12" y2="15"/>'
            "</svg>"
        )
    elif name == "ICON_PLUS":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round">'
            '<line x1="12" y1="5" x2="12" y2="19"/>'
            '<line x1="5" y1="12" x2="19" y2="12"/>'
            "</svg>"
        )
    elif name == "ICON_TRASH":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="3,6 5,6 21,6"/>'
            '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
            '<path d="M10 11v6"/><path d="M14 11v6"/>'
            '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>'
            "</svg>"
        )
    elif name == "ICON_EDIT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
            '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'
            "</svg>"
        )
    elif name == "ICON_REFRESH":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<path d="M20 6v5h-5"/>'
            '<path d="M19.1 13.5a7.5 7.5 0 1 1-2.2-7.1L20 11"/>'
            "</svg>"
        )
    elif name == "ICON_GRIP":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">'
            '<circle cx="9"  cy="5"  r="1.8"/><circle cx="15" cy="5"  r="1.8"/>'
            '<circle cx="9"  cy="12" r="1.8"/><circle cx="15" cy="12" r="1.8"/>'
            '<circle cx="9"  cy="19" r="1.8"/><circle cx="15" cy="19" r="1.8"/>'
            "</svg>"
        )
    elif name == "ICON_EYE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
            '<circle cx="12" cy="12" r="3"/>'
            "</svg>"
        )
    elif name == "ICON_EYE_OFF":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<path d="M3 3l18 18"/><path d="M10.6 5.2A10.7 10.7 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-2.3 3.2"/>'
            '<path d="M6.6 6.6C3.6 8.6 2 12 2 12s3.5 7 10 7c1.7 0 3.2-.5 4.5-1.2"/>'
            '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'
            "</svg>"
        )
    elif name in {"ICON_LOCK", "ICON_UNLOCK"}:
        shackle = (
            '<path d="M7 10V7a5 5 0 0 1 10 0v3"/>'
            if name == "ICON_LOCK"
            else '<path d="M17 10V7a5 5 0 0 0-9.7-1.7"/>'
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            f"{shackle}"
            '<rect x="5" y="10" width="14" height="11" rx="2"/>'
            '<circle cx="12" cy="15.5" r="1" fill="currentColor" stroke="none"/>'
            "</svg>"
        )
    elif name in {"ICON_ALIGN_LEFT", "ICON_ALIGN_CENTER", "ICON_ALIGN_RIGHT"}:
        starts = {
            "ICON_ALIGN_LEFT": (4, 4, 4, 4),
            "ICON_ALIGN_CENTER": (4, 7, 4, 6),
            "ICON_ALIGN_RIGHT": (4, 9, 4, 8),
        }[name]
        ends = {
            "ICON_ALIGN_LEFT": (20, 15, 20, 17),
            "ICON_ALIGN_CENTER": (20, 17, 20, 18),
            "ICON_ALIGN_RIGHT": (20, 20, 20, 20),
        }[name]
        lines = "".join(
            f'<line x1="{start}" y1="{y}" x2="{end}" y2="{y}"/>'
            for start, end, y in zip(starts, ends, (6, 10, 14, 18), strict=True)
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round">'
            f"{lines}</svg>"
        )
    elif name == "ICON_UNDO":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round"><polyline points="9 14 4 9 9 4"/>'
            '<path d="M20 20v-4a7 7 0 0 0-7-7H4"/></svg>'
        )
    elif name == "ICON_REDO":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round"><polyline points="15 14 20 9 15 4"/>'
            '<path d="M4 20v-4a7 7 0 0 1 7-7h9"/></svg>'
        )
    elif name == "ICON_REPEAT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M17 2l4 4-4 4"/>'
            '<path d="M3 11V9a4 4 0 0 1 4-4h14"/>'
            '<path d="M7 22l-4-4 4-4"/>'
            '<path d="M21 13v2a4 4 0 0 1-4 4H3"/>'
            "</svg>"
        )
    elif name == "ICON_SHUFFLE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M16 3h5v5"/><path d="M4 20 21 3"/>'
            '<path d="M21 16v5h-5"/><path d="m15 15 6 6"/>'
            '<path d="m4 4 5 5"/>'
            "</svg>"
        )
    elif name == "ICON_SKIP_NEXT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="5,4 15,12 5,20"/>'
            '<line x1="19" y1="5" x2="19" y2="19"/>'
            "</svg>"
        )
    elif name == "ICON_SKIP_PREV":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="19,4 9,12 19,20"/>'
            '<line x1="5" y1="5" x2="5" y2="19"/>'
            "</svg>"
        )
    elif name == "ICON_PANEL_LEFT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="18" rx="2"/>'
            '<line x1="9" y1="3" x2="9" y2="21"/>'
            "</svg>"
        )
    elif name == "ICON_PANEL_RIGHT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="18" rx="2"/>'
            '<line x1="15" y1="3" x2="15" y2="21"/>'
            "</svg>"
        )
    elif name == "ICON_PANEL_BOTTOM":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="18" rx="2"/>'
            '<line x1="3" y1="15" x2="21" y2="15"/>'
            "</svg>"
        )
    elif name == "ICON_SCREEN":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<path d="M8 21h8"/>'
            '<path d="M12 17v4"/>'
            "</svg>"
        )
    elif name == "ICON_PLAY_ALL":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<polygon points="3,4 13,12 3,20" fill="currentColor"/>'
            '<line x1="16" y1="7" x2="22" y2="7" stroke="currentColor"'
            ' stroke-width="2.5" stroke-linecap="round"/>'
            '<line x1="16" y1="12" x2="22" y2="12" stroke="currentColor"'
            ' stroke-width="2.5" stroke-linecap="round"/>'
            '<line x1="16" y1="17" x2="22" y2="17" stroke="currentColor"'
            ' stroke-width="2.5" stroke-linecap="round"/>'
            "</svg>"
        )
    elif name == "ICON_PLAY_SHUFFLE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
            ' fill="none" stroke="currentColor" stroke-width="2"'
            ' stroke-linecap="round" stroke-linejoin="round">'
            # cross arrows (shuffle shape)
            '<path d="M17 3h4v4"/>'
            '<path d="M3 21l18-18"/>'
            '<path d="M21 17v4h-4"/>'
            '<path d="m15 15 6 6"/>'
            '<path d="m3 3 5 5"/>'
            # small play triangle at bottom-left
            '<polygon points="2,14 2,21 8,17.5" fill="currentColor" stroke="none"/>'
            "</svg>"
        )
    elif name == "ICON_CHEVRON_DOWN":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="6,9 12,15 18,9"/>'
            "</svg>"
        )
    elif name == "ICON_CHEVRON_UP":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="6,15 12,9 18,15"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_LIBRARY":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2.5" y="5" width="5" height="15" rx="1"/>'
            '<path d="M4.2 8h1.6M4.2 17h1.6"/>'
            '<rect x="9" y="3.5" width="5" height="16.5" rx="1"/>'
            '<path d="M10.7 7h1.6M10.7 17h1.6"/>'
            '<g transform="rotate(-10 18 12)"><rect x="15.5" y="3.5" width="5" height="16.5" rx="1"/>'
            '<path d="M17.2 7h1.6M17.2 17h1.6"/></g>'
            "</svg>"
        )
    elif name == "ICON_NAV_BROWSER":
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M4 7c4 2 12 2 16 0M4 17c4-2 12-2 16 0"/><path d="M12 2c5 3 5 17 0 20m0-20c-5 3-5 17 0 20"/></svg>'
    elif name == "ICON_NAV_TIMER":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="13" r="8"/>'
            '<path d="M12 9v4l2.5 2.5"/>'
            '<path d="M9 3h6"/>'
            '<path d="M20 5.27 18.73 4"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_SETTINGS":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="12" r="3"/>'
            '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06'
            "a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09"
            "A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83"
            "l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09"
            "A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83"
            "l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09"
            "a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83"
            "l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09"
            'a1.65 1.65 0 0 0-1.51 1z"/>'
            "</svg>"
        )
    elif name == "ICON_MONITOR":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<path d="M8 21h8"/>'
            '<path d="M12 17v4"/>'
            "</svg>"
        )
    elif name == "ICON_TV":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="7" width="20" height="15" rx="2"/>'
            '<polyline points="17 2 12 7 7 2"/>'
            "</svg>"
        )
    elif name == "ICON_SET_AS_IDLE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<path d="M8 21h8"/>'
            '<path d="M12 17v4"/>'
            '<circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" stroke="none"/>'
            '<polyline points="21,15 16,10 11,15" stroke-width="1.6"/>'
            '<polyline points="13,15 9.5,11.5 5,15" stroke-width="1.6"/>'
            "</svg>"
        )
    elif name == "ICON_CAST":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/>'
            '<path d="M2 15a7 7 0 0 1 7 7"/>'
            '<path d="M2 19a3 3 0 0 1 3 3"/>'
            '<circle cx="2" cy="22" r="1" fill="currentColor" stroke="none"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_THEME":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<path d="M7 8h5"/>'
            '<path d="M7 12h8"/>'
            '<path d="M7 16h3"/>'
            '<line x1="8" y1="21" x2="16" y2="21"/>'
            '<line x1="12" y1="17" x2="12" y2="21"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_PLAYLIST":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.4" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<path d="M2.93 4.35C2.43 4.05 1.8 4.41 1.8 5V9C1.8 9.59 2.43 9.95 2.93 9.65L6.44 7.65C6.95 7.36 6.95 6.64 6.44 6.35L2.93 4.35Z"'
            ' fill="currentColor" stroke="none"/>'
            '<line x1="9.3" y1="7" x2="21" y2="7"/>'
            '<line x1="3" y1="13" x2="21" y2="13"/>'
            '<line x1="3" y1="19" x2="21" y2="19"/>'
            "</svg>"
        )
    elif name == "ICON_ADD_TO_PLAYLIST":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<line x1="12" y1="5" x2="12" y2="19"/>'
            '<line x1="5" y1="12" x2="19" y2="12"/>'
            "</svg>"
        )
    elif name == "ICON_SEND_TO_PLAYLIST":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<line x1="3" y1="6" x2="15" y2="6"/>'
            '<line x1="3" y1="10" x2="15" y2="10"/>'
            '<line x1="3" y1="14" x2="9" y2="14"/>'
            '<polyline points="17,14 20,17 17,20"/>'
            '<line x1="13" y1="17" x2="20" y2="17"/>'
            "</svg>"
        )
    elif name == "ICON_SAVE_PLAYLIST":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
            '<polyline points="17,21 17,13 7,13 7,21"/>'
            '<polyline points="7,3 7,8 15,8"/>'
            "</svg>"
        )
    elif name == "ICON_CROP":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M6 2v14a2 2 0 0 0 2 2h14"/>'
            '<path d="M18 22V8a2 2 0 0 0-2-2H2"/>'
            "</svg>"
        )
    elif name == "ICON_MEDIA_TRIM":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.1" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<circle cx="5.5" cy="6.5" r="3.4"/>'
            '<circle cx="5.5" cy="17.5" r="3.4"/>'
            '<path d="M8.25 8.5 21.5 20.5"/>'
            '<path d="M8.25 15.5 21.5 3.5"/>'
            "</svg>"
        )
    elif name == "ICON_MEDIA_LANGUAGE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
            "<defs>"
            '<mask id="mediaLanguageMask">'
            '<path fill="#fff" d="M0 0h24v24H0z"/>'
            '<rect x="2.25" y="6.25" width="15.5" height="15.5" rx="2.8" fill="#000"/>'
            "</mask>"
            "</defs>"
            '<rect x="6" y="2.75" width="15.5" height="15.5" rx="2.8"'
            ' stroke="currentColor" stroke-width="1.3" opacity=".55"'
            ' mask="url(#mediaLanguageMask)"/>'
            '<rect x="2.25" y="6.25" width="15.5" height="15.5" rx="2.8"'
            ' stroke="currentColor" stroke-width="1.5"/>'
            '<path d="M7.4 10.1 13.9 14l-6.5 3.9z" fill="currentColor"/>'
            "</svg>"
        )
    elif name == "ICON_CLOUD_DOWNLOAD":
        return (
            # SVG fornecido pelo designer — nuvem + seta de download
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M6.5 17a4.5 4.5 0 1 1 .8-8.9 5.5 5.5 0 0 1 9.7.4 3.8 3.8 0 1 1 .8 7.5"/>'
            '<path d="M12 10.7v8.6"/>'
            '<path d="m9.8 17.8 2.2 2.5 2.2-2.5"/>'
            "</svg>"
        )
    elif name == "ICON_MANUAL_DOWNLOAD":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M6.4 16.6a4.15 4.15 0 0 1 .8-8.2 5.05 5.05 0 0 1 9.1.35 3.55 3.55 0 0 1 .9 7.05"/>'
            '<path d="M12 10.6v7.3"/>'
            '<path d="m9.8 15.8 2.2 2.3 2.2-2.3"/>'
            '<path d="M8.7 20.6h6.6"/>'
            "</svg>"
        )
    elif name == "ICON_CLOUD_DONE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M6.5 17a4.5 4.5 0 1 1 .8-8.9 5.5 5.5 0 0 1 9.7.4 3.8 3.8 0 1 1 .8 7.5"/>'
            '<polyline points="9.5,13.5 11.5,15.5 15,11.5"/>'
            "</svg>"
        )
    elif name == "ICON_AUDIO_COVER":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
            "<defs>"
            # Radial glow behind bars
            '<radialGradient id="glow" cx="50%" cy="50%" r="50%">'
            '<stop offset="0%"  stop-color="#1c2a3d" stop-opacity="1"/>'
            '<stop offset="100%" stop-color="#0d1117" stop-opacity="1"/>'
            "</radialGradient>"
            # Bar gradient: bottom accent → top softer
            '<linearGradient id="bar" x1="0" y1="1" x2="0" y2="0">'
            '<stop offset="0%"  stop-color="#388bfd" stop-opacity="0.9"/>'
            '<stop offset="100%" stop-color="#79c0ff" stop-opacity="0.6"/>'
            "</linearGradient>"
            "</defs>"
            # Background
            '<rect width="200" height="200" fill="#0d1117"/>'
            '<rect width="200" height="200" fill="url(#glow)"/>'
            # Waveform bars — 7 bars, symmetric, centered at (100,100)
            # widths=10, gap=8 → total=118 → start x=41
            # heights: 22 50 76 96 76 50 22  →  y = 100 - h/2
            '<rect x="41"  y="89"  width="10" height="22" rx="5" fill="url(#bar)" opacity="0.45"/>'
            '<rect x="59"  y="75"  width="10" height="50" rx="5" fill="url(#bar)" opacity="0.60"/>'
            '<rect x="77"  y="62"  width="10" height="76" rx="5" fill="url(#bar)" opacity="0.78"/>'
            '<rect x="95"  y="52"  width="10" height="96" rx="5" fill="url(#bar)" opacity="1.00"/>'
            '<rect x="113" y="62"  width="10" height="76" rx="5" fill="url(#bar)" opacity="0.78"/>'
            '<rect x="131" y="75"  width="10" height="50" rx="5" fill="url(#bar)" opacity="0.60"/>'
            '<rect x="149" y="89"  width="10" height="22" rx="5" fill="url(#bar)" opacity="0.45"/>'
            "</svg>"
        )
    elif name == "ICON_AUTO_DOWNLOAD":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 3v12"/>'
            '<path d="m7 10 5 5 5-5"/>'
            '<path d="M5 21h14"/>'
            "</svg>"
        )
    elif name == "JS_SVG_IMAGE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"'
            ' viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"'
            ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<line x1="8" y1="21" x2="16" y2="21"/>'
            '<line x1="12" y1="17" x2="12" y2="21"/>'
            '<path d="M6 13 l3-4 2.5 3 2-2.5 3.5 3.5"/>'
            '<circle cx="16.5" cy="7.5" r="1.5" fill="white" stroke="none"/>'
            "</svg>"
        )
    elif name == "JS_SVG_VIDEO":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"'
            ' viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"'
            ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<rect x="2" y="3" width="20" height="14" rx="2"/>'
            '<line x1="8" y1="21" x2="16" y2="21"/>'
            '<line x1="12" y1="17" x2="12" y2="21"/>'
            '<polygon points="10,7.5 10,13.5 16.5,10.5" fill="white" stroke="none"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_WIFI":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M5 12.55a11 11 0 0 1 14.08 0"/>'
            '<path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
            '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>'
            '<line x1="12" y1="20" x2="12.01" y2="20" stroke-width="3" stroke-linecap="round"/>'
            "</svg>"
        )
    elif name == "ICON_NAV_MEETINGS":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>'
            '<line x1="16" y1="2" x2="16" y2="6"/>'
            '<line x1="8" y1="2" x2="8" y2="6"/>'
            '<line x1="3" y1="10" x2="21" y2="10"/>'
            '<path d="M8 14h.01"/>'
            '<path d="M12 14h.01"/>'
            '<path d="M8 18h.01"/>'
            '<path d="M12 18h.01"/>'
            "</svg>"
        )
    elif name == "ICON_HOME":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
            '<polyline points="9 22 9 12 15 12 15 22"/>'
            "</svg>"
        )
    elif name == "ICON_CHEVRON_LEFT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="15 18 9 12 15 6"/>'
            "</svg>"
        )
    elif name == "ICON_CHEVRON_RIGHT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="9 18 15 12 9 6"/>'
            "</svg>"
        )
    elif name == "ICON_CALENDAR":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>'
            '<line x1="16" y1="2" x2="16" y2="6"/>'
            '<line x1="8" y1="2" x2="8" y2="6"/>'
            '<line x1="3" y1="10" x2="21" y2="10"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_PT_BR":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">'
            '<path fill="#259245" d="M0 153.838h800V646.16H0z"/>'
            '<path fill="#ffe000" d="m105.416 399.999 294.584-171.151 294.584 171.151-294.584 171.15z"/>'
            '<path fill="#103b9b" d="M400 283.091c-64.463 0-116.908 52.445-116.908 116.908'
            " 0 64.464 52.445 116.908 116.908 116.908 43.372 0 82.977-23.896 103.358-62.362"
            ' 7.824-16.026 11.792-33.226 11.792-51.119.001-64.462-52.443-116.907-116.907-116.907z"/>'
            '<path fill="#fff" d="M407.138 382.741c-30.816-12.646-64.859-14.257-96.576-4.69'
            " a91.7 91.7 0 0 0-13.083 29.503c31.718-9.567 65.761-7.956 96.578 4.69"
            " 30.771 12.63 56.099 35.35 71.956 64.38a91.5 91.5 0 0 0 11.415-30.16"
            ' 170.15 170.15 0 0 0-73.573-58.094 170.15 170.15 0 0 0-93.293-10.319z"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_EN_US":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
            '<path fill="#b22334" d="M35.445 7C34.752 5.809 33.477 5 32 5H18v2zM0 25h36v2H0z'
            "M18 17h18v2H18zm0-4h18v2H18zM0 21h36v2H0z"
            "M4 35h28c1.477 0 2.752-.809 3.445-2H.555c.693 1.191 1.968 2 3.445 2"
            'M18 9h18v2H18z"/>'
            '<path fill="#eee" d="M.068 27.679q.025.14.059.277.04.15.092.296c.089.259.197.509.333.743L.555 29'
            "h34.89a4 4 0 0 0 .332-.741 4 4 0 0 0 .152-.576C35.972 27.462 36 27.236 36 27H0"
            "c0 .233.028.458.068.679M0 23h36v2H0zm0-4v2h36v-2H18zm18-4h18v2H18zm0-4h18v2H18z"
            'M18 9h18c0-.233-.028-.459-.069-.68a3.6 3.6 0 0 0-.153-.576A4 4 0 0 0 35.445 7H18z"/>'
            '<path fill="#3c3b6e" d="M18 5H4a4 4 0 0 0-4 4v10h18z"/>'
            '<path fill="#fff" d="m2.001 7.726.618.449-.236.725L3 8.452l.618.448-.236-.725L4 7.726h-.764'
            "L3 7l-.235.726zm2 2 .618.449-.236.725.617-.448.618.448-.236-.725L6 9.726h-.764L5 9l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L9 9l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L13 9l-.235.726z"
            "m-8 4 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L5 13l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L9 13l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L13 13l-.235.726z"
            "m-6-6 .618.449-.236.725L7 8.452l.618.448-.236-.725L8 7.726h-.764L7 7l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L11 7l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L15 7l-.235.726z"
            "m-12 4 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L3 11l-.235.726z"
            "M6.383 12.9 7 12.452l.618.448-.236-.725.618-.449h-.764L7 11l-.235.726h-.764l.618.449z"
            "m3.618-1.174.618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L11 11l-.235.726z"
            "m4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L15 11l-.235.726z"
            "m-12 4 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L3 15l-.235.726z"
            "M6.383 16.9 7 16.452l.618.448-.236-.725.618-.449h-.764L7 15l-.235.726h-.764l.618.449z"
            "m3.618-1.174.618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L11 15l-.235.726z"
            'm4 0 .618.449-.236.725.617-.448.618.448-.236-.725.618-.449h-.764L15 15l-.235.726z"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_ES":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">'
            '<path fill="#ed1f34" d="M0 0h800v267H0z"/>'
            '<path fill="#ffe000" d="M0 267h800v266H0z"/>'
            '<path fill="#ed1f34" d="M0 533h800v267H0z"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_FR_FR":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 600">'
            '<path fill="#002395" d="M0 0h300v600H0z"/>'
            '<path fill="#fff" d="M300 0h300v600H300z"/>'
            '<path fill="#ed2939" d="M600 0h300v600H600z"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_IT_IT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 600">'
            '<path fill="#009246" d="M0 0h300v600H0z"/>'
            '<path fill="#fff" d="M300 0h300v600H300z"/>'
            '<path fill="#ce2b37" d="M600 0h300v600H600z"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_ZH_CN":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 600">'
            '<path fill="#de2910" d="M0 0h900v600H0z"/>'
            # Grande estrela
            '<polygon fill="#ffde00" points="150,100 175,178 100,128 200,128 125,178"/>'
            # 4 estrelas menores
            '<polygon fill="#ffde00" points="270,50 279,77 255,60 285,60 261,77"/>'
            '<polygon fill="#ffde00" points="310,90 319,117 295,100 325,100 301,117"/>'
            '<polygon fill="#ffde00" points="310,150 319,177 295,160 325,160 301,177"/>'
            '<polygon fill="#ffde00" points="270,190 279,217 255,200 285,200 261,217"/>'
            "</svg>"
        )
    elif name == "ICON_FLAG_JA_JP":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 600">'
            '<path fill="#fff" d="M0 0h900v600H0z"/>'
            '<circle fill="#bc002d" cx="450" cy="300" r="180"/>'
            "</svg>"
        )
    elif name == "ICON_OBS":
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor">'
            '<path d="M12 24C5.383 24 0 18.617 0 12S5.383 0 12 0s12 5.383 12 12-5.383 12-12 12m0-22.891C5.995 1.109 1.11 5.995 1.11 12S5.995 22.89 12 22.89 22.89 18.005 22.89 12 18.005 1.109 12 1.109M6.182 5.99c.352-1.698 1.503-3.229 3.05-3.996-.269.273-.595.483-.844.78-1.02 1.1-1.48 2.692-1.199 4.156.355 2.235 2.455 4.06 4.732 4.028 1.765.079 3.485-.937 4.348-2.468 1.848.063 3.645 1.017 4.7 2.548.54.799.962 1.736.991 2.711-.342-1.295-1.202-2.446-2.375-3.095a4.9 4.9 0 0 0-3.772-.425c-1.56.448-2.849 1.723-3.293 3.293-.377 1.25-.216 2.628.377 3.772-.825 1.429-2.315 2.449-3.932 2.756-1.244.261-2.551.059-3.709-.464 1.036.302 2.161.355 3.191-.011a4.91 4.91 0 0 0 3.024-2.935c.556-1.49.345-3.261-.591-4.54-.7-1.007-1.803-1.717-3.002-1.969-.38-.068-.764-.098-1.148-.134-.611-1.231-.834-2.66-.528-3.996z"/>'
            "</svg>"
        )
    elif name == "ICON_REC_CIRCLE":
        return '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8"/></svg>'
    elif name == "ICON_REC_STOP":
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor">'
            '<rect x="6" y="6" width="12" height="12" rx="2"/>'
            "</svg>"
        )
    elif name in {"ICON_FOLDER", "ICON_FOLDER_LINK"}:
        link = (
            ""
            if name == "ICON_FOLDER"
            else (
                '  <path d="M10 13H8a1 1 0 0 0 0 2h2"/>\n'
                '  <path d="M14 13h2a1 1 0 0 1 0 2h-2"/>\n'
                '  <line x1="11" y1="14" x2="13" y2="14"/>\n'
            )
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">\n'
            '  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>\n'
            f"{link}"
            "</svg>"
        )
    elif name == "ICON_ZOOM":
        return (
            '<svg viewBox="0 1 20 20" fill="none" stroke="currentColor" stroke-width="1.5" '
            'stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M2.267 5.5C1.567 5.5 1 6.14 1 6.929v6.928C1 15.87 2.446 17.5 4.23 17.5l7.503-.071'
            "c.7 0 1.267-.64 1.267-1.429v-7c0-2.012-1.716-3.5-3.5-3.5zm12.445 2.728C14.26 8.675 "
            "14 9.32 14 10v2.9c0 .678.26 1.324.712 1.772l2.817 2.48c.573.567 1.471.108 1.471-.751"
            'V6.635c0-.86-.898-1.318-1.471-.752z"/>'
            "</svg>"
        )
    elif name == "ICON_PEOPLE":
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3z'
            "m-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3z"
            "m0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5z"
            'm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>'
            "</svg>"
        )
    elif name == "ICON_INFO_CIRCLE":
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z'
            'm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>'
            "</svg>"
        )
    elif name == "ICON_SPEAKER_PHONE":
        return (
            '<svg viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M20 15.5c-1.25 0-2.45-.2-3.57-.57a1.02 1.02 0 0 0-1.02.24l-2.2 2.2'
            "a15.045 15.045 0 0 1-6.59-6.59l2.2-2.21a.96.96 0 0 0 .25-1A11.36 11.36 0 0 1"
            " 8.5 4c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1 0 9.39 7.61 17 17 17 .55 0 1-.45"
            ' 1-1v-3.5c0-.55-.45-1-1-1z"'
            "/>"
            '<path d="M19 12h2c0-4.97-4.03-9-9-9v2c3.87 0 7 3.13 7 7z"/>'
            '<path d="M15 12h2c0-2.76-2.24-5-5-5v2c1.66 0 3 1.34 3 3z"/>'
            "</svg>"
        )
    elif name == "ICON_OVERLAY_CLOSE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
            '<circle cx="12" cy="12" r="10.5"'
            '  stroke="currentColor" stroke-width="1.1" stroke-opacity="0.35"'
            '  fill="currentColor" fill-opacity="0.08"/>'
            '<path d="M15.2 8.8 L8.8 15.2 M8.8 8.8 L15.2 15.2"'
            '  stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>'
            "</svg>"
        )
    elif name == "ICON_BOOK":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>'
            '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'
            "</svg>"
        )
    elif name == "ICON_PLUG":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 22v-5"/>'
            '<path d="M9 8V2"/>'
            '<path d="M15 8V2"/>'
            '<path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>'
            "</svg>"
        )
    elif name == "ICON_CLAPPERBOARD":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<rect x="3" y="10.5" width="18" height="10.5" rx="2"/>'
            '<line x1="7" y1="14.5" x2="12" y2="14.5"/>'
            '<line x1="7" y1="17.5" x2="15" y2="17.5"/>'
            '<g transform="rotate(-10 3 10.5)">'
            '<rect x="3" y="5.5" width="18" height="5" rx="1.5"/>'
            '<line x1="8" y1="5.5" x2="10" y2="10.5"/>'
            '<line x1="13.5" y1="5.5" x2="15.5" y2="10.5"/>'
            "</g></svg>"
        )
    elif name == "ICON_PACKAGE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
            'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
            '<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>'
            '<line x1="12" y1="22.08" x2="12" y2="12"/>'
            "</svg>"
        )
    elif name == "ICON_KEYBOARD":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="2" y="5" width="20" height="14" rx="2"/>'
            '<path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01"/>'
            '<path d="M6 13h.01M10 13h.01M14 13h.01M18 13h.01"/>'
            '<path d="M8 17h8"/>'
            "</svg>"
        )
    elif name == "ICON_CROSSHAIR":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="12" r="10"/>'
            '<circle cx="12" cy="12" r="4"/>'
            '<line x1="12" y1="2" x2="12" y2="6"/>'
            '<line x1="12" y1="18" x2="12" y2="22"/>'
            '<line x1="2" y1="12" x2="6" y2="12"/>'
            '<line x1="18" y1="12" x2="22" y2="12"/>'
            "</svg>"
        )
    elif name == "ICON_COPY":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round"><rect x="8" y="8" width="12" height="12" rx="2"/>'
            '<path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>'
        )
    elif name == "ICON_CHECK":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
            ' stroke-linejoin="round"><path d="m5 12.5 4.2 4.2L19 7"/></svg>'
        )
    elif name == "ICON_SHIELD":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round"><path d="M12 3 20 6v5c0 5.1-3.4 8.7-8 10"/>'
            '<path d="M12 3 4 6v5c0 5.1 3.4 8.7 8 10"/><path d="m9 12 2 2 4-4"/></svg>'
        )
    elif name == "ICON_REMOTE_CONTROL":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round"><rect x="3" y="2" width="11" height="20" rx="2"/>'
            '<path d="M6.5 18h4"/><path d="M15.75 9.5a2.5 2.5 0 0 1 0 5"/>'
            '<path d="M17 7a4 5 0 0 1 0 10"/></svg>'
        )
    elif name == "ICON_SHARE_SCREEN":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="4" width="18" height="13" rx="2"/>'
            '<path d="M8 21h8"/>'
            '<path d="M12 17v4"/>'
            '<path d="M12 13V8"/>'
            '<path d="m9 11 3-3 3 3"/>'
            "</svg>"
        )
    elif name == "ICON_ASPECT_MATCH":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="18" rx="2.5"/>'
            '<path d="M8 11V8.75A.75.75 0 0 1 8.75 8H11"/>'
            '<path d="M16 13v2.25a.75.75 0 0 1-.75.75H13"/>'
            "</svg>"
        )
    elif name == "ICON_BOUNDS":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
            ' stroke-linejoin="round">'
            '<rect x="4" y="4" width="16" height="16" rx="2"/>'
            '<path d="M9 9h6v6H9z"/>'
            '<path d="M9 3v3M15 3v3M9 18v3M15 18v3"/>'
            '<path d="M3 9h3M18 9h3M3 15h3M18 15h3"/>'
            "</svg>"
        )
    elif name == "ICON_FULLSCREEN":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
            '<path d="M16 3h3a2 2 0 0 1 2 2v3"/>'
            '<path d="M21 16v3a2 2 0 0 1-2 2h-3"/>'
            '<path d="M8 21H5a2 2 0 0 1-2-2v-3"/>'
            "</svg>"
        )
    elif name == "ICON_FULLSCREEN_EXIT":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M8 3v3a2 2 0 0 1-2 2H3"/>'
            '<path d="M16 3v3a2 2 0 0 0 2 2h3"/>'
            '<path d="M21 16h-3a2 2 0 0 0-2 2v3"/>'
            '<path d="M3 16h3a2 2 0 0 1 2 2v3"/>'
            "</svg>"
        )
    elif name == "ICON_SECTION":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="3" y="3" width="18" height="7" rx="2"/>'
            '<rect x="3" y="14" width="18" height="7" rx="2"/>'
            "</svg>"
        )
    elif name == "ICON_MARKER":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M5 5h14"/>'
            '<path d="M5 19h14"/>'
            '<rect x="7" y="8" width="10" height="8" rx="2"/>'
            '<path d="M10 12h4"/>'
            "</svg>"
        )
    elif name == "ICON_PALETTE":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 297" xml:space="preserve" fill="currentColor">'
            '<path d="M254.141 53.244C224.508 18.909 185.299 0 143.736 0c-35.062 0-68.197 13.458-93.302 37.9-40.051 38.992-53.256 85.382-36.227 127.278 13.868 34.122 45.625 57.954 77.227 57.954.841 0 1.671-.016 2.508-.053 4.705-.194 9.249-.586 13.646-.966 5.309-.462 10.325-.895 14.77-.895 10.54 0 19.645 0 19.645 26.846 0 28.811 17.538 48.934 42.65 48.936h.004c17.864 0 37.651-10.342 57.215-29.903 25.882-25.88 43.099-62.198 47.234-99.64 4.656-42.131-7.763-82.694-34.965-114.213M227.315 252.54c-15.397 15.398-30.55 23.877-42.66 23.875-16.288 0-22.064-15.274-22.064-28.352 0-32.357-12.786-47.43-40.232-47.43-5.333 0-10.778.472-16.545.969-4.169.359-8.481.733-12.724.909a38 38 0 0 1-1.655.034c-23.07 0-47.529-18.975-58.156-45.118C19.565 123.689 31.054 85.5 64.798 52.648c21.239-20.676 49.272-32.063 78.939-32.063 35.485 0 69.159 16.373 94.82 46.107 50.63 58.667 34.043 140.564-11.242 185.848"/>'
            '<path d="M192.654 165.877c0 17.213 13.918 31.217 31.026 31.217 17.107 0 31.025-14.004 31.025-31.217 0-17.215-13.918-31.219-31.025-31.219-17.108 0-31.026 14.004-31.026 31.219m41.464 0c0 5.861-4.682 10.633-10.438 10.633s-10.438-4.771-10.438-10.633c0-5.863 4.683-10.633 10.438-10.633s10.438 4.77 10.438 10.633m-7.204-72.388c0-17.215-13.917-31.219-31.025-31.219s-31.025 14.004-31.025 31.219c0 17.211 13.918 31.218 31.025 31.218 17.108 0 31.025-14.007 31.025-31.218m-41.464 0c0-5.865 4.684-10.632 10.439-10.632 5.756 0 10.438 4.767 10.438 10.632 0 5.86-4.683 10.633-10.438 10.633S185.45 99.35 185.45 93.489m-60.587-53.862c-17.107 0-31.025 14.004-31.025 31.217s13.918 31.217 31.025 31.217 31.025-14.004 31.025-31.217-13.918-31.217-31.025-31.217m0 41.851c-5.756 0-10.438-4.771-10.438-10.634s4.682-10.633 10.438-10.633 10.438 4.77 10.438 10.633c-.001 5.863-4.682 10.634-10.438 10.634M70.821 92.809c-17.107 0-31.026 14.004-31.026 31.217s13.919 31.219 31.026 31.219 31.024-14.005 31.024-31.219-13.917-31.217-31.024-31.217m0 41.849c-5.757 0-10.439-4.77-10.439-10.633 0-5.861 4.683-10.63 10.439-10.63 5.755 0 10.438 4.769 10.438 10.63 0 5.864-4.683 10.633-10.438 10.633"/>'
            "</svg>"
        )
    raise AttributeError(name)


def __getattr__(name: str):
    if name in _ICON_NAMES:
        svg = _SVG_CACHE.get(name)
        if svg is None:
            svg = _build_icon_svg(name)
            _SVG_CACHE[name] = svg
        return svg
    if name == "FLAG_ICONS":
        module = sys.modules[__name__]
        return {code: getattr(module, icon_name) for code, icon_name in _FLAG_ICON_NAMES.items()}
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _ICON_NAMES | {"FLAG_ICONS"})


__all__ = sorted(
    _ICON_NAMES
    | {
        "USE_EMOJI_FLAGS",
        "resolve_emoji_font",
        "make_emoji_flag_icon",
        "make_flag_icon",
        "make_icon",
        "get_flag_icon",
        "FLAG_ICONS",
    }
)


# Ícone "abrir painel de playlist" — retângulo com linha vertical direita


# ── Sidebar navigation icons ──────────────────────────────────────────────────


# ── Set as Idle Screen icon (monitor with photo/landscape inside) ─────────────

# ── Cast / Project tab icon ───────────────────────────────────────────────────


# ── Cache / offline icons ──────────────────────────────────────────────────────


# Kept for potential future use elsewhere.

# ── Audio cover art fallback ───────────────────────────────────────────────────


# ── Auto-download / cache settings icon ───────────────────────────────────────


# ── JS-embeddable SVGs (para o overlay do browser) ────────────────────────────
# Importados em widgets/browser/widget.py e injetados no OVERLAY_JS como strings JS.


# ── Cache / Media Manager nav icon ────────────────────────────────────────────


# ── Wi-Fi Receive nav icon ─────────────────────────────────────────────────────


# ── Meetings / navigation icons ──────────────────────────────────────────────


# ── Flag icons (multi-color SVG — renderizados com make_flag_icon) ────────────


# Ícone circular para botão de fechar overlay (ex: troca de perfil).
# Círculo sutil de fundo + X com linhas arredondadas — estilo moderno.

# ── Onboarding / profile-screen icons ────────────────────────────────────────


# Mapeamento código → SVG de bandeira (usado quando USE_EMOJI_FLAGS = False)


def _get_flag_svg(lang_code: str) -> str | None:
    icon_name = _FLAG_ICON_NAMES.get(lang_code)
    if not icon_name:
        return None
    return getattr(sys.modules[__name__], icon_name)


def get_flag_icon(lang_code: str, emoji: str, size: int = 24) -> QIcon:
    """
    Retorna o QIcon de bandeira para *lang_code* respeitando USE_EMOJI_FLAGS.

    Quando USE_EMOJI_FLAGS = True:
        Usa *emoji* (ex: '🇧🇷') renderizado com a fonte de emoji do sistema.
        Funciona para qualquer idioma sem precisar de SVG novo.

    Quando USE_EMOJI_FLAGS = False:
        Usa o SVG hardcoded de FLAG_ICONS[lang_code] quando disponível.
        Idiomas sem SVG caem em fallback de texto emoji (QLabel).

    Parâmetros
    ----------
    lang_code : str   código do idioma (ex: 'pt_BR', 'zh_CN')
    emoji     : str   emoji de bandeira do JSON meta['flag'] (ex: '🇧🇷')
    size      : int   tamanho do ícone em pixels
    """
    if USE_EMOJI_FLAGS:
        return make_emoji_flag_icon(emoji, size)

    svg = _get_flag_svg(lang_code)
    if svg:
        return make_flag_icon(svg, size)

    # Fallback SVG não disponível → tenta emoji mesmo assim
    return make_emoji_flag_icon(emoji, size)


# ── Playlist section icons ────────────────────────────────────────────────────


# ── Meeting-section artwork (centralized) ─────────────────────────────────────
# Shared by the Meetings detail view and the Timer tab. Each uses
# ``currentColor`` so it can be tinted per-section by make_icon / image providers.
# Midweek: diamond (Treasures), wheat (Field Ministry), sheep (Living as
# Christians). Weekend: lectern (Public Talk) and watchtower (Watchtower Study).

ICON_SEC_TREASURES = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M6 4 h12 l4 6 L12 22 L2 10 Z M2 10 h20 M6 4 L12 22 M18 4 L12 22'
    ' M8 10 L12 4 L16 10"/></svg>'
)

ICON_SEC_MINISTRY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 49 61">'
    '<g fill="currentColor">'
    '<path d="M20.89 14.5c.36-3.29-2.07-8.82 2.28-10.08.09 2.99.8 6.18-.12 9.09-.54.25-1.62.74-2.16.99'
    "m-4.43-2.8c-.56-2.24-.47-4.55 2.24-5.03-.2 1.97 1.15 6.21-2.24 5.03"
    "m8.87.54c.29-1.8-.92-6.74 2.35-4.77.51 2.15.36 4.61-2.35 4.77"
    "m-14.24 7.67c-2.47-2.65-1.6-6.68-.93-9.88 3.45 2.03 7.26 3.76 10.03 6.72 1.57 2.75.72 6.1.37 9.07"
    "-3.27-1.76-6.6-3.53-9.47-5.91m.59-5.98c-.21 4.46 4.03 6.23 7.31 8 .22-4.5-4.04-6.2-7.31-8"
    "m12.16 2.95c2.86-3.09 6.74-5.16 10.66-6.62-.11 3.19 1.01 7.15-1.51 9.7-2.91 2.4-6.23 4.33-9.76 5.67"
    ".08-2.88-.68-6.07.61-8.75m8.17-2.72c-3.23 1.59-7.05 3.53-6.83 7.77 2.83-1.84 8-3.36 6.83-7.77"
    "M9.69 22.5c3.67 1.65 7.4 3.51 10.32 6.32 1.81 2.79.84 6.34.66 9.44-3.5-2.02-7.33-3.74-10.19-6.66"
    "-1.64-2.72-.73-6.1-.79-9.1m1.95 3.77c-.2 4.46 4 6.22 7.27 8.02.22-4.42-4.06-6.2-7.27-8.02"
    "m12.39 2.63c2.94-2.85 6.7-4.73 10.39-6.41-.05 3.33 1.27 7.69-1.79 10.11-2.86 2.05-5.77 4.35-9.21 5.3"
    '-.31-2.95-.92-6.3.61-9m1.24 5.47c3.2-1.9 7.41-3.65 7.23-8.12-3.23 1.86-7.56 3.62-7.23 8.12"/>'
    '<path d="M20.46 50.41c-7.05-1.1-12.97-8.65-10.22-15.71 2.91 1.78 5.9 3.47 8.65 5.49'
    " 2.69 1.64 1.93 5.17 2.33 7.81h1.8c.33-2.4-.49-5.47 1.57-7.26 2.95-2.44 6.36-4.26 9.72-6.06"
    " 1.83 7.23-3.4 14.61-10.66 15.74-.3 1.65-.02 3.67-1.67 4.67-1.39-1.17-1.22-3.06-1.52-4.68"
    "m-8.77-11.8c1.07 3.82 3.37 7.21 7.05 8.91-.1-1.57-.33-3.13-.67-4.66-1.85-1.79-4.2-2.93-6.38-4.25"
    'm15.27 3.45c-1.9 1.12-1.1 3.65-1.51 5.46 3.59-1.79 5.97-5.07 6.96-8.92-1.85 1.1-3.75 2.13-5.45 3.46"/>'
    "</g></svg>"
)

ICON_SEC_LIVING = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 111 108">'
    '<g fill="currentColor">'
    '<path d="M72.35 21.29c5.01-3.3 12.92-3.64 17.05 1.26 2.86 3.26 6.09 6.2 8.7 9.66'
    " 1.08 3.18-1.07 6.51-3.54 8.39-2.17 1.77-5.12 1.3-7.71 1.48-1.41 6.97-2.85 13.94-4.24 20.91"
    "-0.7 4.13-3.59 7.52-7.04 9.72.87 7.37 2.37 14.72 2.15 22.17-3.47.02-7.03.53-10.46-.25"
    "-1.88-4.85-2.87-10-4.2-15.02-1.15 5.23-2.9 10.3-4.69 15.34-3.38.03-6.84.39-10.13-.57"
    ".62-6.47 1.63-12.9 2.37-19.36-3.48-.05-7-.25-10.41.56 1.36 6.37 3.54 12.55 4.48 19.01"
    "-3.62.56-7.29.47-10.94.32-1.76-4.35-3.62-8.68-4.88-13.22-.99 4.27-1.73 8.61-3.07 12.78"
    "-2.99.98-6.37.68-9.46.19l-.57-1.01c-.12-7.16-.39-14.38.13-21.51 2.25-2.68 4.98-4.9 7.48-7.34"
    ".02-1.81.05-3.61.09-5.41-2.19 1.4-5.04 5.19-7.71 2.55-.48-4.66-.4-9.41-.02-14.08"
    " 1.13-7.67 8.39-14.09 16.2-13.87 8.07-.05 16.14.09 24.21-.02 1.34.15 2.28-.98 3.26-1.68"
    " 4.29-3.7 8.49-7.52 12.95-11m1.94 4.02c-4.96 4.01-9.58 8.46-14.61 12.38-9.2.83-18.54-.08-27.78.42"
    "-4.97.36-9.4 3.98-11.1 8.6-.7 3.21-.36 6.53-.5 9.79 2.09-1.39 5.17-5.21 7.55-2.44"
    ".96 3.85.16 8.04.43 12.03-2.57 2.62-5.23 5.15-7.89 7.69-.03 5.86-.07 11.72.02 17.58"
    ".38-.18 1.13-.53 1.51-.7 1.4-3.81 1.48-8.02 3.09-11.76 4.22-2.53 8.75-4.52 13.09-6.83"
    " 2.4-1.44 5.27-1.04 7.94-1.09 6.42.15 12.85-.24 19.27.26 2.01 6.47 3.51 13.1 5.7 19.5"
    ".54.13 1.61.4 2.15.53-.54-6.95-2.23-13.86-1.89-20.84 2.46-2.12 5.85-3.87 6.46-7.39"
    " 1.72-7.61 3.11-15.3 4.83-22.92.37-3.19 4.32-1.75 6.46-2.19 2.69.13 4.37-2.25 4.87-4.61"
    "-3.01-2.8-5.69-5.91-8.7-8.7-3.12-2.71-7.88-1.6-10.9.69M54.88 75.3c-.09 5.39-1.16 10.7-1.52 16.07"
    "l1.51-.69c2.04-5.12 3.41-10.51 5.07-15.78-1.69.11-3.38.24-5.06.4"
    "m-21.87 4.31c1.22 3.7 2.46 7.43 4.12 10.96.57.19 1.73.56 2.31.75-1.07-4.42-2.3-8.81-3.3-13.25"
    '-1.04.51-2.08 1.02-3.13 1.54"/>'
    '<path d="M74.38 30.37c1.66-1.12 3.36.09 4.4 1.42-.75 1.03-1.5 2.05-2.24 3.08l.24 2.92'
    'c.95 1.24 2.21 2.48 1.82 4.21-1.23 1.72-3.92 1.1-4.89-.57-2.28-3.13-2.38-8.38.67-11.06"/>'
    "</g></svg>"
)

# Weekend — Public Talk: speaker at a lectern.
ICON_SEC_PUBLIC_TALK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="7" r="3"/>'
    '<path d="M6 17a6 6 0 0 1 12 0"/>'
    '<path d="M4 17h16l-2 4H6z"/>'
    "</svg>"
)

# Weekend — Watchtower Study: clean watchtower silhouette with arched opening.
ICON_SEC_WATCHTOWER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 24" fill="none"'
    ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4.149 1.398 H8.169 V4.668 H10.572 V1.398 H14.636 V4.668'
    " H17.039 V1.398 H21.103 V4.668 H23.55 V1.398 H27.57 V8.332"
    ' L23.754 13.063 V22 H7.965 V13.063 L4.149 8.332 Z"/>'
    "</svg>"
)
