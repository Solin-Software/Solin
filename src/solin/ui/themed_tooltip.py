from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QHelpEvent, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

from solin.styles.theme import PALETTE


class _ThemedTooltipPopup(QFrame):
    _SHADOW_PAD = 8
    _SHADOW_OFFSET_Y = 2
    _BUBBLE_RADIUS = 6
    _TEXT_MARGIN_X = 8
    _TEXT_MARGIN_Y = 5

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
        self._label = QLabel(self)
        self._label.setWordWrap(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            self._SHADOW_PAD + self._TEXT_MARGIN_X,
            self._SHADOW_PAD + self._TEXT_MARGIN_Y,
            self._SHADOW_PAD + self._TEXT_MARGIN_X,
            self._SHADOW_PAD + self._TEXT_MARGIN_Y,
        )
        layout.addWidget(self._label)

    def visual_offset(self) -> QPoint:
        return QPoint(self._SHADOW_PAD, self._SHADOW_PAD)

    def set_text(self, text: str) -> None:
        self._label.setText(text)
        self._apply_theme()
        self.adjustSize()
        self.update()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            "QLabel {"
            f" color: {PALETTE.text_primary};"
            " background: transparent;"
            " font-size: 12px;"
            "}"
        )

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
        painter.end()


_POPUP: _ThemedTooltipPopup | None = None


def _popup() -> _ThemedTooltipPopup:
    global _POPUP
    if _POPUP is None:
        _POPUP = _ThemedTooltipPopup()
    return _POPUP


def show_themed_tooltip(global_pos: QPoint, text: str) -> None:
    if not text:
        hide_themed_tooltip()
        return
    popup = _popup()
    popup.set_text(text)
    popup.move(global_pos - popup.visual_offset())
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
            pos = event.globalPos() if isinstance(event, QHelpEvent) else watched.mapToGlobal(
                QPoint(watched.width() // 2, 0)
            )
            show_themed_tooltip(pos + QPoint(0, 18), text)
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
    existing = getattr(widget, "_solin_themed_tooltip_filter", None)
    if existing is None:
        existing = ThemedTooltipFilter(widget)
        widget.installEventFilter(existing)
        widget._solin_themed_tooltip_filter = existing
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(hide_themed_tooltip)
