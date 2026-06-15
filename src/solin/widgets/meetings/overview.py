from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.date import format_single_date
from ...styles.icons import ICON_CHEVRON_RIGHT, make_icon
from .visuals import (
    MEETING_ACCENT,
    MEETING_BG,
    MEETING_BORDER,
    MEETING_CARD,
    MEETING_DANGER,
    MEETING_GOLD,
    MEETING_MUTED,
    MEETING_PURPLE,
    MEETING_TEXT,
    MEETING_WARNING,
    rounded_meeting_pixmap,
)

if TYPE_CHECKING:
    from ...core.meetings.models import MemorialData, WeekData


class _PubCard(QFrame):
    open_requested = Signal()

    _CW, _CH = 80, 108

    def __init__(self, pub_type: str, parent=None):
        super().__init__(parent)
        self._pub_type = pub_type
        self._is_mwb = pub_type == "mwb"
        self._last_pct: int = -1
        self._progress_style_set = False
        self.setObjectName("PubCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(148)
        self._apply_style(active=False)
        self._build()
        self.set_loading()

    def _apply_style(self, active: bool):
        accent = MEETING_ACCENT if self._is_mwb else MEETING_PURPLE
        border = accent if active else MEETING_BORDER
        self.setStyleSheet(
            f"QFrame#PubCard{{background:{MEETING_CARD};"
            f"border:1px solid {border};border-radius:16px;}}"
            f"QFrame#PubCard:hover{{background:#1a1f2b;"
            f"border-color:{accent};}}"
        )

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 16, 14)
        outer.setSpacing(14)

        self._cover = QLabel()
        self._cover.setFixedSize(self._CW, self._CH)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet(
            f"background:#21262d;border-radius:8px;border:1px solid {MEETING_BORDER};"
        )
        outer.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        right = QWidget()
        right.setStyleSheet("background:transparent;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 2, 0, 2)
        rl.setSpacing(0)

        pill_color = MEETING_ACCENT if self._is_mwb else MEETING_PURPLE
        self._pill = QLabel("")
        self._pill.setStyleSheet(
            f"background:transparent;color:{pill_color};"
            "font-size:9px;font-weight:800;letter-spacing:1px;"
        )
        rl.addWidget(self._pill)
        rl.addSpacing(6)

        self._title_lbl = QLabel("")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(
            f"color:{MEETING_TEXT};font-size:15px;font-weight:700;background:transparent;"
        )
        self._title_lbl.setMaximumWidth(260)
        rl.addWidget(self._title_lbl)
        rl.addSpacing(4)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setWordWrap(True)
        self._sub_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:11px;background:transparent;"
        )
        self._sub_lbl.setMaximumWidth(260)
        rl.addWidget(self._sub_lbl)

        rl.addStretch(1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(4)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        arrow_col = MEETING_ACCENT if self._is_mwb else MEETING_PURPLE
        self._arrow = QLabel()
        self._arrow.setPixmap(
            make_icon(ICON_CHEVRON_RIGHT, 13, arrow_col).pixmap(13, 13)
        )
        self._arrow.setStyleSheet("background:transparent;")
        bot.addWidget(self._status_lbl)
        bot.addStretch()
        bot.addWidget(self._arrow)
        rl.addLayout(bot)

        outer.addWidget(right, 1, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit()
        super().mousePressEvent(event)

    def set_loading(self):
        self._last_pct = -1
        self._progress_style_set = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Loading…"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Fetching publication…"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)

    def set_ready(self, wd: "WeekData"):
        self._last_pct = -1
        self._progress_style_set = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        if self._is_mwb:
            title = wd.mwb_date_label or wd.mwb_week_title or self.tr("Life & Ministry")
            cover_bytes = wd.mwb_cover_bytes
            n_items = len(wd.mwb_all_media) + len(wd.cbs_items)
        else:
            title = wd.wt_study_title or self.tr("Watchtower Study")
            cover_bytes = wd.wt_cover_bytes
            n_items = len(wd.wt_all_media)

        self._title_lbl.setText(title)
        self._sub_lbl.setText("")
        word = self.tr("item") if n_items == 1 else self.tr("items")
        self._status_lbl.setText(f"{n_items} media {word}")
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

        if cover_bytes:
            pix = QPixmap()
            pix.loadFromData(cover_bytes)
            if not pix.isNull():
                self._cover.setPixmap(rounded_meeting_pixmap(pix, self._CW, self._CH, 8))
                return
        ph = QPixmap(self._CW, self._CH)
        ph.fill(QColor("#21262d"))
        self._cover.setPixmap(ph)

    def set_empty(self):
        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("No meeting this week"))
        self._sub_lbl.setText("")
        self._status_lbl.setText("")
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_error(self, _msg: str = ""):
        self._last_pct = -1
        self._progress_style_set = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Unavailable"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_DANGER};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

    def set_not_found(self):
        self._last_pct = -1
        self._progress_style_set = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Not available this week"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_WARNING};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

    def set_progress(self, pct: int):
        if pct == self._last_pct:
            return
        self._last_pct = pct

        if not self._progress_style_set:
            self._status_lbl.setStyleSheet(
                f"color:{MEETING_ACCENT};font-size:10px;background:transparent;"
            )
            self._arrow.setVisible(False)
            self._progress_style_set = True

        self._status_lbl.setText(self.tr("Downloading… {}%").format(pct))


class _MemorialCard(QFrame):
    open_requested = Signal()

    _CW, _CH = 80, 108

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_pct: int = -1
        self._progress_style_set = False
        self.setObjectName("MemCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(148)
        self._apply_style(active=False)
        self._build()
        self.set_loading()

    def _apply_style(self, active: bool):
        border = MEETING_GOLD if active else MEETING_BORDER
        self.setStyleSheet(
            f"QFrame#MemCard{{background:{MEETING_CARD};"
            f"border:1px solid {border};border-radius:16px;}}"
            f"QFrame#MemCard:hover{{background:#1a1a10;"
            f"border-color:{MEETING_GOLD};}}"
        )

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 16, 14)
        outer.setSpacing(14)

        self._cover = QLabel()
        self._cover.setFixedSize(self._CW, self._CH)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet(
            f"background:#21262d;border-radius:8px;border:1px solid {MEETING_BORDER};"
        )
        outer.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        right = QWidget()
        right.setStyleSheet("background:transparent;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 2, 0, 2)
        rl.setSpacing(0)

        self._pill = QLabel("")
        self._pill.setStyleSheet(
            f"background:transparent;color:{MEETING_GOLD};"
            "font-size:9px;font-weight:800;letter-spacing:1px;"
        )
        rl.addWidget(self._pill)
        rl.addSpacing(6)

        self._title_lbl = QLabel("")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(
            f"color:{MEETING_TEXT};font-size:15px;font-weight:700;background:transparent;"
        )
        self._title_lbl.setMaximumWidth(260)
        rl.addWidget(self._title_lbl)
        rl.addSpacing(4)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setWordWrap(True)
        self._sub_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:11px;background:transparent;"
        )
        self._sub_lbl.setMaximumWidth(260)
        rl.addWidget(self._sub_lbl)

        rl.addStretch(1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(4)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._arrow = QLabel()
        self._arrow.setPixmap(
            make_icon(ICON_CHEVRON_RIGHT, 13, MEETING_GOLD).pixmap(13, 13)
        )
        self._arrow.setStyleSheet("background:transparent;")
        bot.addWidget(self._status_lbl)
        bot.addStretch()
        bot.addWidget(self._arrow)
        rl.addLayout(bot)

        outer.addWidget(right, 1, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit()
        super().mousePressEvent(event)

    def set_loading(self):
        self._last_pct = -1
        self._progress_style_set = False
        self._pill.setText(self.tr("MEMORIAL"))
        self._title_lbl.setText(self.tr("Loading…"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Fetching publication…"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)
        self._apply_style(active=False)

    def set_ready(self, md: "MemorialData"):
        self._last_pct = -1
        self._progress_style_set = False
        self._pill.setText(self.tr("MEMORIAL"))

        date_str = format_single_date(md.memorial_date) if md.memorial_date else ""

        self._title_lbl.setText(self.tr("Memorial of Jesus’ Death"))
        self._sub_lbl.setText(date_str)
        n = len(md.videos)
        word = self.tr("item") if n == 1 else self.tr("items")
        self._status_lbl.setText(f"{n} media {word}")
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)
        self._apply_style(active=True)

        if md.cover_bytes:
            pix = QPixmap()
            pix.loadFromData(md.cover_bytes)
            if not pix.isNull():
                self._cover.setPixmap(rounded_meeting_pixmap(pix, self._CW, self._CH, 8))
                return
        ph = QPixmap(self._CW, self._CH)
        ph.fill(QColor("#2a2200"))
        self._cover.setPixmap(ph)

    def set_empty(self):
        self._pill.setText(self.tr("MEMORIAL"))
        self._title_lbl.setText(self.tr("Memorial of Jesus’ Death"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("No media found"))
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)
        self._apply_style(active=False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_error(self):
        self._last_pct = -1
        self._progress_style_set = False
        self._pill.setText(self.tr("MEMORIAL"))
        self._title_lbl.setText(self.tr("Memorial of Jesus’ Death"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_DANGER};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)
        self._apply_style(active=False)

    def set_not_found(self):
        self._last_pct = -1
        self._progress_style_set = False
        self._pill.setText(self.tr("MEMORIAL"))
        self._title_lbl.setText(self.tr("Memorial of Jesus’ Death"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Not available · Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_WARNING};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)
        self._apply_style(active=False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_deleted(self):
        self._last_pct = -1
        self._progress_style_set = False
        self._pill.setText(self.tr("MEMORIAL"))
        self._title_lbl.setText(self.tr("Memorial of Jesus’ Death"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Media removed by JW.ORG"))
        self._status_lbl.setStyleSheet(
            f"color:{MEETING_MUTED};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(False)
        self._apply_style(active=False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_progress(self, pct: int):
        if pct == self._last_pct:
            return
        self._last_pct = pct
        if not self._progress_style_set:
            self._status_lbl.setStyleSheet(
                f"color:{MEETING_GOLD};font-size:10px;background:transparent;"
            )
            self._arrow.setVisible(False)
            self._progress_style_set = True
        self._status_lbl.setText(self.tr("Downloading… {}%").format(pct))

    def set_not_yet(self):
        self.setVisible(False)


class _Overview(QWidget):
    open_mwb = Signal()
    open_wt = Signal()
    open_memorial = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{MEETING_BG};")
        self._build()

    def _build(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:{MEETING_BG};}}"
            "QScrollBar:vertical{width:4px;background:transparent;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:2px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        content = QWidget()
        content.setStyleSheet(f"background:{MEETING_BG};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(16, 12, 16, 24)
        cl.setSpacing(12)

        self.mwb_card = _PubCard("mwb")
        self.mwb_card.open_requested.connect(self.open_mwb)

        self.wt_card = _PubCard("wt")
        self.wt_card.open_requested.connect(self.open_wt)

        self.memorial_card = _MemorialCard()
        self.memorial_card.open_requested.connect(self.open_memorial)
        self.memorial_card.setVisible(False)

        cl.addWidget(self.mwb_card)
        cl.addWidget(self.wt_card)
        cl.addWidget(self.memorial_card)
        cl.addStretch()
        scroll.setWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)
