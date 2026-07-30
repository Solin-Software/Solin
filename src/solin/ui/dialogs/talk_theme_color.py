"""Color picker integration for the talk-theme editor."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QColorDialog,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleFactory,
    QStyleOptionSpinBox,
    QWidget,
)

from solin.core.talk_theme.settings import (
    DEFAULT_CUSTOM_COLOR,
    merge_custom_colors,
    normalize_custom_colors,
)
from solin.styles.theme import PALETTE


class _SpinBoxArrowOverlay(QWidget):
    """Paint visible arrows without intercepting the native step buttons."""

    def __init__(self, spin: QSpinBox) -> None:
        super().__init__(spin)
        self._spin = spin
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setGeometry(spin.rect())
        spin.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._spin and event.type() in {
            QEvent.Type.LayoutRequest,
            QEvent.Type.Resize,
            QEvent.Type.Show,
        }:
            self.setGeometry(self._spin.rect())
            self.raise_()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        option = QStyleOptionSpinBox()
        self._spin.initStyleOption(option)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(PALETTE.text_secondary), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        for control, upward in (
            (QStyle.SubControl.SC_SpinBoxUp, True),
            (QStyle.SubControl.SC_SpinBoxDown, False),
        ):
            rect = self._spin.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox,
                option,
                control,
                self._spin,
            )
            center = rect.center()
            points = (
                QPolygon(
                    [
                        QPoint(center.x() - 3, center.y() + 2),
                        QPoint(center.x(), center.y() - 1),
                        QPoint(center.x() + 3, center.y() + 2),
                    ]
                )
                if upward
                else QPolygon(
                    [
                        QPoint(center.x() - 3, center.y() - 2),
                        QPoint(center.x(), center.y() + 1),
                        QPoint(center.x() + 3, center.y() - 2),
                    ]
                )
            )
            painter.drawPolyline(points)
        painter.end()


class TalkThemeColorDialog(QColorDialog):
    """Expose deterministic, profile-backed custom colors with usable inputs."""

    def __init__(
        self,
        initial: QColor,
        custom_colors: tuple[str, ...],
        title: str,
        parent: QWidget | None = None,
        *,
        show_alpha: bool = True,
    ) -> None:
        super().__init__(initial, parent)
        self._stored_custom_colors = normalize_custom_colors(custom_colors)
        self.setWindowTitle(title)
        self.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, show_alpha)
        self.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        self._load_custom_colors()
        self._spin_style = QStyleFactory.create("Fusion")
        if self._spin_style is not None:
            self._spin_style.setParent(self)
        self._spin_overlays: list[_SpinBoxArrowOverlay] = []
        for spin in self.findChildren(QSpinBox):
            if self._spin_style is not None:
                spin.setStyle(self._spin_style)
            self._spin_overlays.append(_SpinBoxArrowOverlay(spin))
        for button in self.findChildren(QPushButton):
            button.clicked.connect(self._schedule_custom_color_reconcile)

    def _load_custom_colors(self) -> None:
        for index in range(self.customCount()):
            color = (
                self._stored_custom_colors[index]
                if index < len(self._stored_custom_colors)
                else DEFAULT_CUSTOM_COLOR
            )
            self.setCustomColor(index, QColor(color))

    def _schedule_custom_color_reconcile(self, _checked: bool = False) -> None:
        # QColorDialog has no signal for custom-palette changes. All action buttons
        # are public QPushButtons, so defer one cheap comparison until Qt finishes
        # its own click handler; non-palette buttons become no-ops.
        QTimer.singleShot(0, self._reconcile_custom_colors)

    def _reconcile_custom_colors(self) -> None:
        observed = tuple(
            self.customColor(index).name(QColor.NameFormat.HexRgb).upper()
            for index in range(self.customCount())
        )
        reconciled = merge_custom_colors(
            self._stored_custom_colors,
            observed,
            capacity=self.customCount(),
        )
        if reconciled == self._stored_custom_colors:
            return
        self._stored_custom_colors = reconciled
        self._load_custom_colors()

    def custom_colors(self) -> tuple[str, ...]:
        self._reconcile_custom_colors()
        return self._stored_custom_colors


__all__ = ["TalkThemeColorDialog"]
