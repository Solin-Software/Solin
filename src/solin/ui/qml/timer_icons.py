"""QML image provider for timer SVG icons."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer

from solin.styles.icons import (
    ICON_BAN,
    ICON_BOOK,
    ICON_CALENDAR,
    ICON_CHECK,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_CHEVRON_UP,
    ICON_CLOSE,
    ICON_COPY,
    ICON_EDIT,
    ICON_EXPORT,
    ICON_GRIP,
    ICON_HOME,
    ICON_INFO_CIRCLE,
    ICON_LOCK,
    ICON_MARKER,
    ICON_MICROPHONE,
    ICON_MONITOR,
    ICON_MORE_HORIZ,
    ICON_MUSIC,
    ICON_NAV_SETTINGS,
    ICON_NAV_TIMER,
    ICON_PEOPLE,
    ICON_PLAY,
    ICON_PLUS,
    ICON_RAISED_HAND,
    ICON_RAISED_HAND_FILLED,
    ICON_REC_STOP,
    ICON_REFRESH,
    ICON_REPEAT,
    ICON_SEC_LIVING,
    ICON_SEC_MINISTRY,
    ICON_SEC_PUBLIC_TALK,
    ICON_SEC_TREASURES,
    ICON_SEC_WATCHTOWER,
    ICON_SKIP_NEXT,
    ICON_SLIDERS,
    ICON_TIMER_SECTION,
    ICON_TRASH,
    ICON_VIDEO,
)
from solin.styles.theme import PALETTE

# Names the timer QML can request via ``image://timericons/<name>/<size>/<hex>``.
_TIMER_ICON_MAP = {
    "treasures": ICON_SEC_TREASURES,
    "ministry": ICON_SEC_MINISTRY,
    "living": ICON_SEC_LIVING,
    "public_talk": ICON_SEC_PUBLIC_TALK,
    "watchtower": ICON_SEC_WATCHTOWER,
    "section_group": ICON_TIMER_SECTION,
    "ban": ICON_BAN,
    "book": ICON_BOOK,
    "people": ICON_PEOPLE,
    "microphone": ICON_MICROPHONE,
    "music": ICON_MUSIC,
    "video": ICON_VIDEO,
    "marker": ICON_MARKER,
    "check": ICON_CHECK,
    "clock": ICON_NAV_TIMER,
    "calendar": ICON_CALENDAR,
    "monitor": ICON_MONITOR,
    "play": ICON_PLAY,
    "stop": ICON_REC_STOP,
    "reset": ICON_REPEAT,
    "refresh": ICON_REFRESH,
    "raised_hand": ICON_RAISED_HAND,
    "raised_hand_filled": ICON_RAISED_HAND_FILLED,
    "export": ICON_EXPORT,
    "home": ICON_HOME,
    "settings": ICON_NAV_SETTINGS,
    "close": ICON_CLOSE,
    "chevron_left": ICON_CHEVRON_LEFT,
    "chevron_right": ICON_CHEVRON_RIGHT,
    "chevron_down": ICON_CHEVRON_DOWN,
    "chevron_up": ICON_CHEVRON_UP,
    "plus": ICON_PLUS,
    "edit": ICON_EDIT,
    "trash": ICON_TRASH,
    "copy": ICON_COPY,
    "more_horiz": ICON_MORE_HORIZ,
    "grip": ICON_GRIP,
    "skip_next": ICON_SKIP_NEXT,
    "sliders": ICON_SLIDERS,
    "info": ICON_INFO_CIRCLE,
    "lock": ICON_LOCK,
}


class TimerIconProvider(QQuickImageProvider):
    """Serve the timer tab SVG icons, tinted and aspect-fit."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_str: str, size, requestedSize):  # noqa: N802
        parts = id_str.split("/")
        name = parts[0] if parts else ""
        px = max(1, int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else 16
        color = QColor(f"#{parts[2]}") if len(parts) > 2 else QColor(PALETTE.text_secondary)
        if not color.isValid():
            color = QColor(PALETTE.text_secondary)

        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)

        svg_str = _TIMER_ICON_MAP.get(name)
        if not svg_str:
            return pix

        renderer = QSvgRenderer(QByteArray(svg_str.replace("currentColor", color.name()).encode()))
        if not renderer.isValid():
            return pix

        view_box = renderer.defaultSize()
        view_width = view_box.width() or px
        view_height = view_box.height() or px
        scale = min(px / view_width, px / view_height)
        width = view_width * scale
        height = view_height * scale
        target = QRectF((px - width) / 2.0, (px - height) / 2.0, width, height)

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, target)
        if color.alpha() != 255:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.fillRect(pix.rect(), QColor(0, 0, 0, color.alpha()))
        painter.end()
        return pix
