from __future__ import annotations

from datetime import date, timedelta

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
from ...core.meetings.meeting_weeks import current_monday
from ...styles.icons import (
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_HOME,
    make_icon,
)
from .visuals import (
    MEETING_ACCENT,
    MEETING_BG,
    MEETING_BORDER,
    MEETING_MUTED,
    MEETING_SUBTLE_TEXT,
    MEETING_SUCCESS,
    MEETING_TEXT,
)

__all__ = (
    "WeekNavBar",
    "WeekPicker",
)


def _ghost_btn(
    svg: str,
    size: int = 30,
    icon_px: int = 15,
    color: str = MEETING_MUTED,
    tip: str = "",
) -> QPushButton:
    btn = QPushButton()
    btn.setFixedSize(size, size)
    btn.setIcon(make_icon(svg, icon_px, color))
    btn.setIconSize(QSize(icon_px, icon_px))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        btn.setToolTip(tip)
    btn.setStyleSheet(
        "QPushButton{border:none;background:transparent;border-radius:8px;padding:0;}"
        "QPushButton:hover{background:#1c2128;}"
        "QPushButton:pressed{background:#13161c;}"
    )
    return btn


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
        weeks = [today_mon + timedelta(weeks=i) for i in range(-2, 6)]

        card = QFrame()
        card.setObjectName("WkCard")
        card.setStyleSheet(
            "QFrame#WkCard{"
            "background:#1c2128;"
            "border:1px solid #30363d;"
            "border-radius:16px;"
            "}"
        )

        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 14, 0, 10)
        cl.setSpacing(0)

        hdr = QLabel(self.tr("Jump to week"))
        hdr.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;font-weight:700;"
            "letter-spacing:1.2px;background:transparent;"
            "padding-left:16px;padding-right:16px;"
        )
        cl.addWidget(hdr)
        cl.addSpacing(10)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{MEETING_BORDER};border:none;")
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
                "color:#ffffff;font-size:12px;font-weight:700;"
                "background:transparent;"
            )
        elif is_now:
            date_lbl.setStyleSheet(
                f"color:{MEETING_TEXT};font-size:12px;font-weight:600;"
                "background:transparent;"
            )
        elif is_past:
            date_lbl.setStyleSheet(
                f"color:{MEETING_MUTED};font-size:12px;font-weight:400;"
                "background:transparent;"
            )
        else:
            date_lbl.setStyleSheet(
                f"color:{MEETING_SUBTLE_TEXT};font-size:12px;font-weight:500;"
                "background:transparent;"
            )

        hl.addWidget(date_lbl, 1, Qt.AlignmentFlag.AlignVCenter)

        if is_now and is_sel:
            badge = QLabel(self.tr("Now"))
            badge.setStyleSheet(
                "color:#7eb8ff;"
                "background:#0d3a80;"
                "font-size:9px;font-weight:700;"
                "letter-spacing:0.5px;"
                "border-radius:5px;"
                "padding:2px 7px;"
            )
            hl.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        elif is_now:
            badge = QLabel(self.tr("Now"))
            badge.setStyleSheet(
                f"color:{MEETING_SUCCESS};"
                "background:#0f2d16;"
                "border:1px solid #1a5c2a;"
                "font-size:9px;font-weight:700;"
                "letter-spacing:0.5px;"
                "border-radius:5px;"
                "padding:2px 7px;"
            )
            hl.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        if is_sel:
            row.setStyleSheet(
                "QWidget#WkRow{"
                "background:#1f6feb;"
                "border-radius:10px;"
                "margin-left:6px;margin-right:6px;"
                "}"
                "QWidget#WkRow:hover{"
                "background:#2a7bf5;"
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
                "background:#21262d;"
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

    _MAX_BACK = 2
    _MAX_FORWARD = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(68)
        self.setStyleSheet(f"background:{MEETING_BG};")
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(0)

        self.home_btn = _ghost_btn(ICON_HOME, 32, 16, MEETING_MUTED, self.tr("This week"))
        self.home_btn.clicked.connect(self.home_requested)
        self.prev_btn = _ghost_btn(ICON_CHEVRON_LEFT, 32, 16, MEETING_MUTED, self.tr("Previous week"))
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
            f"color:{MEETING_TEXT};font-size:15px;font-weight:700;background:transparent;"
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

        self.next_btn = _ghost_btn(ICON_CHEVRON_RIGHT, 32, 16, MEETING_MUTED, self.tr("Next week"))
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
        today_mon = current_monday()
        is_now = monday == today_mon
        display = db_label or week_label(monday)
        self._date_lbl.setText(display)

        if is_now:
            self._badge_lbl.setText(self.tr("This week"))
            self._badge_lbl.setStyleSheet(
                f"background:transparent;color:{MEETING_SUCCESS};font-size:10px;font-weight:600;"
            )
            self._badge_lbl.setVisible(True)
        else:
            self._badge_lbl.setVisible(False)

        home_color = MEETING_ACCENT if not is_now else MEETING_MUTED
        self.home_btn.setIcon(make_icon(ICON_HOME, 16, home_color))

        self.prev_btn.setEnabled(monday > today_mon - timedelta(weeks=self._MAX_BACK))
        self.next_btn.setEnabled(monday < today_mon + timedelta(weeks=self._MAX_FORWARD))

        prev_col = MEETING_SUBTLE_TEXT if self.prev_btn.isEnabled() else "#484f58"
        next_col = MEETING_SUBTLE_TEXT if self.next_btn.isEnabled() else "#484f58"
        self.prev_btn.setIcon(make_icon(ICON_CHEVRON_LEFT, 16, prev_col))
        self.next_btn.setIcon(make_icon(ICON_CHEVRON_RIGHT, 16, next_col))
