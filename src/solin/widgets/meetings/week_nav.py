from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.date import week_label, week_label_short
from ...core.meetings.meeting_weeks import (
    current_monday,
    meeting_week_bounds,
    selectable_meeting_weeks,
)
from ...styles.icons import (
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_HOME,
    make_icon,
)
from ...styles.theme import PALETTE
__all__ = (
    "WeekNavBar",
    "WeekPicker",
)


def _ghost_btn(
    svg: str,
    size: int = 30,
    icon_px: int = 15,
    color: str | None = None,
    tip: str = "",
) -> QPushButton:
    color = color or PALETTE.text_muted
    btn = QPushButton()
    btn.setFixedSize(size, size)
    btn.setIcon(make_icon(svg, icon_px, color))
    btn.setIconSize(QSize(icon_px, icon_px))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        btn.setToolTip(tip)
    _apply_ghost_button_style(btn)
    return btn


def _apply_ghost_button_style(btn: QPushButton) -> None:
    btn.setStyleSheet(
        "QPushButton{border:none;background:transparent;border-radius:8px;padding:0;}"
        f"QPushButton:hover{{background:{PALETTE.surface_hover_strong};}}"
        f"QPushButton:pressed{{background:{PALETTE.surface_alt};}}"
    )


class WeekPicker(QWidget):
    week_selected = Signal(object)  # date

    def __init__(self, selected: date, cache: dict | None = None, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._selected = selected
        self._cache = cache or {}
        self._build()

    def _build(self):
        today_mon = current_monday()
        weeks = selectable_meeting_weeks()

        card = QFrame()
        card.setObjectName("WkCard")
        card.setStyleSheet(
            "QFrame#WkCard{"
            f"background:{PALETTE.surface};"
            f"border:1px solid {PALETTE.border};"
            "border-radius:16px;"
            "}"
        )

        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 14, 0, 10)
        cl.setSpacing(0)

        hdr = QLabel(self.tr("Jump to week"))
        hdr.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;font-weight:700;"
            "letter-spacing:1.2px;background:transparent;"
            "padding-left:16px;padding-right:16px;"
        )
        cl.addWidget(hdr)
        cl.addSpacing(10)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{PALETTE.border_muted};border:none;")
        cl.addWidget(sep)
        cl.addSpacing(6)

        for w in weeks:
            is_sel = w == self._selected
            is_now = w == today_mon
            is_past = w < today_mon
            cl.addWidget(self._make_row(w, is_sel, is_now, is_past))

        cl.addSpacing(4)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)
        self.adjustSize()

    def _make_row(self, w: date, is_sel: bool, is_now: bool, is_past: bool) -> QWidget:
        wd = self._cache.get(w.isoformat())
        lbl_text = (
            wd.mwb_date_label or wd.mwb_week_title or week_label_short(w)
            if wd
            else week_label_short(w)
        )

        row = QWidget()
        row.setObjectName("WkRow")
        row.setFixedHeight(38)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setContentsMargins(0, 0, 0, 0)

        hl = QHBoxLayout(row)
        hl.setContentsMargins(10, 0, 12, 0)
        hl.setSpacing(8)

        date_lbl = QLabel(lbl_text)
        if is_sel:
            date_lbl.setStyleSheet(
                f"color:{PALETTE.text_on_accent};font-size:12px;font-weight:700;"
                "background:transparent;"
            )
        elif is_now:
            date_lbl.setStyleSheet(
                f"color:{PALETTE.text_primary};font-size:12px;font-weight:600;"
                "background:transparent;"
            )
        elif is_past:
            date_lbl.setStyleSheet(
                f"color:{PALETTE.text_muted};font-size:12px;font-weight:400;"
                "background:transparent;"
            )
        else:
            date_lbl.setStyleSheet(
                f"color:{PALETTE.text_secondary};font-size:12px;font-weight:500;"
                "background:transparent;"
            )

        hl.addWidget(date_lbl, 1, Qt.AlignmentFlag.AlignVCenter)

        if is_now and is_sel:
            badge = QLabel(self.tr("Now"))
            badge.setStyleSheet(
                f"color:{PALETTE.accent_text_hover};"
                f"background:{PALETTE.accent_muted_hover};"
                "font-size:9px;font-weight:700;"
                "letter-spacing:0.5px;"
                "border-radius:5px;"
                "padding:2px 7px;"
            )
            hl.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        elif is_now:
            badge = QLabel(self.tr("Now"))
            badge.setStyleSheet(
                f"color:{PALETTE.success};"
                f"background:{PALETTE.success_surface};"
                f"border:1px solid {PALETTE.success_border};"
                "font-size:9px;font-weight:700;"
                "letter-spacing:0.5px;"
                "border-radius:5px;"
                "padding:2px 7px;"
            )
            hl.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        if is_sel:
            row.setStyleSheet(
                "QWidget#WkRow{"
                f"background:{PALETTE.accent_selection};"
                "border-radius:10px;"
                "margin-left:6px;margin-right:6px;"
                "}"
                "QWidget#WkRow:hover{"
                f"background:{PALETTE.accent_hover};"
                "border-radius:10px;"
                "margin-left:6px;margin-right:6px;"
                "}"
            )
        else:
            row.setStyleSheet(
                "QWidget#WkRow{"
                "background:transparent;"
                "border-radius:10px;"
                "margin-left:6px;margin-right:6px;"
                "}"
                "QWidget#WkRow:hover{"
                f"background:{PALETTE.surface_hover};"
                "border-radius:10px;"
                "margin-left:6px;margin-right:6px;"
                "}"
            )

        row.mousePressEvent = lambda e, d=w: (
            self._pick(d) if e.button() == Qt.MouseButton.LeftButton else None
        )
        return row

    def _pick(self, monday: date):
        self.week_selected.emit(monday)
        self.close()

    def show_near(self, anchor: QWidget):
        self.adjustSize()
        gp = anchor.mapToGlobal(anchor.rect().bottomLeft())
        x = gp.x() - self.width() // 2 + anchor.width() // 2
        y = gp.y() + 8
        screen = QApplication.screenAt(gp)
        if screen:
            sg = screen.availableGeometry()
            x = max(sg.left() + 8, min(x, sg.right() - self.width() - 8))
            y = max(sg.top() + 8, min(y, sg.bottom() - self.height() - 8))
        self.move(x, y)
        self.show()
        self.raise_()


class WeekNavBar(QWidget):
    prev_week = Signal()
    next_week = Signal()
    home_requested = Signal()
    pick_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(68)
        self._last_monday: date | None = None
        self._last_db_label = ""
        self.apply_theme()
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(0)

        self.home_btn = _ghost_btn(ICON_HOME, 32, 16, PALETTE.text_muted, self.tr("This week"))
        self.home_btn.clicked.connect(self.home_requested)
        self.prev_btn = _ghost_btn(ICON_CHEVRON_LEFT, 32, 16, PALETTE.text_muted, self.tr("Previous week"))
        self.prev_btn.clicked.connect(self.prev_week)

        left = QWidget()
        left.setStyleSheet("background:transparent;")
        ll = QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)
        ll.addWidget(self.home_btn)
        ll.addWidget(self.prev_btn)

        centre = QWidget()
        centre.setStyleSheet("background:transparent;")
        centre.setCursor(Qt.CursorShape.PointingHandCursor)
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(1)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._date_lbl = QLabel("")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._date_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:15px;font-weight:700;background:transparent;"
        )

        self._badge_lbl = QLabel("")
        self._badge_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge_lbl.setStyleSheet("background:transparent;font-size:10px;font-weight:600;")
        self._badge_lbl.setVisible(False)

        cl.addWidget(self._date_lbl)
        cl.addWidget(self._badge_lbl)

        centre.mousePressEvent = lambda e: (
            self.pick_requested.emit()
            if e.button() == Qt.MouseButton.LeftButton else None
        )

        self.next_btn = _ghost_btn(ICON_CHEVRON_RIGHT, 32, 16, PALETTE.text_muted, self.tr("Next week"))
        self.next_btn.clicked.connect(self.next_week)

        right = QWidget()
        right.setStyleSheet("background:transparent;")
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        rl.addWidget(self.next_btn)

        lay.addWidget(left)
        lay.addStretch()
        lay.addWidget(centre)
        lay.addStretch()
        lay.addWidget(right)

    def update_week(self, monday: date, db_label: str = ""):
        self._last_monday = monday
        self._last_db_label = db_label
        today_mon = current_monday()
        oldest_week, newest_week = meeting_week_bounds(today_mon)
        is_now = monday == today_mon
        display = db_label or week_label(monday)
        self._date_lbl.setText(display)

        if is_now:
            self._badge_lbl.setText(self.tr("This week"))
            self._badge_lbl.setStyleSheet(
                f"background:transparent;color:{PALETTE.success};font-size:10px;font-weight:600;"
            )
            self._badge_lbl.setVisible(True)
        else:
            self._badge_lbl.setVisible(False)

        home_color = PALETTE.accent if not is_now else PALETTE.text_muted
        self.home_btn.setIcon(make_icon(ICON_HOME, 16, home_color))

        self.prev_btn.setEnabled(monday > oldest_week)
        self.next_btn.setEnabled(monday < newest_week)

        prev_col = PALETTE.text_secondary if self.prev_btn.isEnabled() else PALETTE.text_dim
        next_col = PALETTE.text_secondary if self.next_btn.isEnabled() else PALETTE.text_dim
        self.prev_btn.setIcon(make_icon(ICON_CHEVRON_LEFT, 16, prev_col))
        self.next_btn.setIcon(make_icon(ICON_CHEVRON_RIGHT, 16, next_col))

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background:{PALETTE.bg0};")
        for button in (
            getattr(self, "home_btn", None),
            getattr(self, "prev_btn", None),
            getattr(self, "next_btn", None),
        ):
            if button is not None:
                _apply_ghost_button_style(button)
        if self._last_monday is not None:
            self.update_week(self._last_monday, self._last_db_label)
