from __future__ import annotations

from math import ceil

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QPainter,
    QPalette,
    QScreen,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QFrame, QWidget

from solin.styles.theme import PALETTE

_SCREEN_MARGIN = 8
_ANCHOR_GAP = 12
_CURSOR_CLEARANCE = 16


class _ThemedTooltipPopup(QFrame):
    _SHADOW_PAD = 8
    _SHADOW_OFFSET_Y = 2
    _BUBBLE_RADIUS = 6
    _TEXT_MARGIN_X = 8
    _TEXT_MARGIN_Y = 5
    _MAX_WIDTH = 420

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setObjectName("ThemedTooltipPopup")
        self._document = QTextDocument(self)
        self._document.setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._document.setDefaultTextOption(option)

    def set_text(self, text: str, maximum_size: QSize) -> bool:
        font = QFont(self.font())
        font.setPixelSize(12)
        self._document.setDefaultFont(font)
        self._document.setPlainText(text)
        self.setAccessibleName(text)
        horizontal_padding = 2 * (self._SHADOW_PAD + self._TEXT_MARGIN_X)
        vertical_padding = 2 * (self._SHADOW_PAD + self._TEXT_MARGIN_Y)
        if maximum_size.width() <= horizontal_padding or maximum_size.height() <= vertical_padding:
            return False
        self._document.setTextWidth(-1)
        width = min(
            ceil(self._document.idealWidth()) + horizontal_padding,
            self._MAX_WIDTH,
            maximum_size.width(),
        )
        self._document.setTextWidth(max(1, width - horizontal_padding))
        self._limit_text_height(maximum_size.height() - vertical_padding)
        height = ceil(self._document.size().height()) + vertical_padding
        if height > maximum_size.height():
            return False
        self.setFixedSize(width, height)
        self.update()
        return True

    def _limit_text_height(self, maximum_height: int) -> None:
        if self._document.size().height() <= maximum_height:
            return
        # If even wrapped content exceeds the screen, keep whole lines and an
        # ellipsis. Qt line boundaries preserve graphemes and surrogate pairs.
        last_line_start = 0
        block = self._document.begin()
        layout = self._document.documentLayout()
        while block.isValid():
            block_y = layout.blockBoundingRect(block).top()
            text_layout = block.layout()
            for index in range(text_layout.lineCount()):
                line = text_layout.lineAt(index)
                if block_y + line.y() + line.height() > maximum_height:
                    cursor = QTextCursor(self._document)
                    cursor.setPosition(last_line_start)
                    cursor.movePosition(
                        QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor
                    )
                    cursor.insertText("…")
                    return
                last_line_start = block.position() + line.textStart()
            block = block.next()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        bubble = QRectF(self.rect()).adjusted(
            self._SHADOW_PAD,
            self._SHADOW_PAD,
            -self._SHADOW_PAD,
            -self._SHADOW_PAD,
        )
        shadow_color = QColor(0, 0, 0)
        for spread, alpha in ((5, 10), (3, 16), (1, 26)):
            shadow_color.setAlpha(alpha)
            painter.setBrush(shadow_color)
            painter.drawRoundedRect(
                bubble.adjusted(-spread, -spread, spread, spread).translated(
                    0,
                    self._SHADOW_OFFSET_Y,
                ),
                self._BUBBLE_RADIUS + spread,
                self._BUBBLE_RADIUS + spread,
            )

        painter.setBrush(QColor(PALETTE.surface_overlay))
        painter.drawRoundedRect(bubble, self._BUBBLE_RADIUS, self._BUBBLE_RADIUS)
        painter.translate(
            self._SHADOW_PAD + self._TEXT_MARGIN_X,
            self._SHADOW_PAD + self._TEXT_MARGIN_Y,
        )
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor(PALETTE.text_primary))
        self._document.documentLayout().draw(painter, context)
        painter.end()


_POPUP: _ThemedTooltipPopup | None = None


def _popup() -> _ThemedTooltipPopup:
    global _POPUP
    if _POPUP is None:
        _POPUP = _ThemedTooltipPopup()
    return _POPUP


def _tooltip_position(
    anchor: QRect, size: QSize, available: QRect, cursor: QPoint
) -> QPoint | None:
    # Wide titles and sliders stay near the pointer; compact controls are centered.
    center_x = (
        cursor.x()
        if anchor.width() > size.width() and anchor.contains(cursor)
        else anchor.center().x()
    )
    x = max(
        available.left(), min(center_x - size.width() // 2, available.right() - size.width() + 1)
    )
    candidates = (
        anchor.top() - size.height() - _ANCHOR_GAP,
        anchor.bottom() + 1 + _ANCHOR_GAP,
        cursor.y() - size.height() - _CURSOR_CLEARANCE,
        cursor.y() + _CURSOR_CLEARANCE + 1,
    )
    for y in candidates:
        rect = QRect(QPoint(x, y), size)
        if available.contains(rect) and not rect.adjusted(
            -_CURSOR_CLEARANCE, -_CURSOR_CLEARANCE, _CURSOR_CLEARANCE, _CURSOR_CLEARANCE
        ).contains(cursor):
            return rect.topLeft()
    for y in candidates:
        y = max(available.top(), min(y, available.bottom() - size.height() + 1))
        if not QRect(QPoint(x, y), size).contains(cursor):
            return QPoint(x, y)
    return None


def show_themed_tooltip(anchor: QRect, text: str, *, screen: QScreen | None = None) -> None:
    if not text:
        hide_themed_tooltip()
        return
    cursor = QCursor.pos()
    reference = cursor if anchor.contains(cursor) else anchor.center()
    screen = QGuiApplication.screenAt(reference) or screen or QGuiApplication.primaryScreen()
    if screen is None:
        hide_themed_tooltip()
        return
    available = screen.availableGeometry().adjusted(
        _SCREEN_MARGIN, _SCREEN_MARGIN, -_SCREEN_MARGIN, -_SCREEN_MARGIN
    )
    if not available.isValid():
        hide_themed_tooltip()
        return
    popup = _popup()
    above_end = anchor.top() - _ANCHOR_GAP
    below_start = anchor.bottom() + 1 + _ANCHOR_GAP
    if available.contains(cursor):
        above_end = min(above_end, cursor.y() - _CURSOR_CLEARANCE)
        below_start = max(below_start, cursor.y() + _CURSOR_CLEARANCE + 1)
    maximum_height = min(
        available.height(),
        max(above_end - available.top(), available.bottom() + 1 - below_start),
    )
    if not popup.set_text(text, QSize(available.width(), maximum_height)):
        hide_themed_tooltip()
        return
    position = _tooltip_position(anchor, popup.size(), available, cursor)
    if position is None:
        hide_themed_tooltip()
        return
    # Create the hidden native window before applying the final position;
    # native window creation can adjust its initial geometry.
    popup.winId()
    popup.move(position)
    popup.show()
    popup.raise_()


def hide_themed_tooltip() -> None:
    if _POPUP is not None:
        _POPUP.hide()


class ThemedTooltipFilter(QObject):
    def eventFilter(self, watched, event) -> bool:
        if not isinstance(watched, QWidget):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.ToolTip:
            text = watched.toolTip()
            if not text:
                hide_themed_tooltip()
                return True
            anchor = QRect(watched.mapToGlobal(QPoint()), watched.size())
            show_themed_tooltip(anchor, text, screen=watched.screen())
            return True
        if event.type() in (
            QEvent.Type.Leave,
            QEvent.Type.Hide,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.WindowDeactivate,
        ):
            hide_themed_tooltip()
        return super().eventFilter(watched, event)


def install_themed_tooltip(widget: QWidget) -> None:
    existing = widget.findChild(
        ThemedTooltipFilter, options=Qt.FindChildOption.FindDirectChildrenOnly
    )
    if existing is not None:
        return
    widget.installEventFilter(ThemedTooltipFilter(widget))
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(hide_themed_tooltip)
