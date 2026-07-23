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
from ...styles.theme import PALETTE
from .visuals import (
    MEETING_PURPLE,
    rounded_meeting_pixmap,
)

if TYPE_CHECKING:
    from ...core.meetings.tree_store import MeetingTreeSnapshot
    from ...core.meetings.models import MemorialData, WeekData

__all__ = ("Overview",)


class _PubCard(QFrame):
    open_requested = Signal()

    _CW, _CH = 80, 108

    def __init__(self, pub_type: str, parent=None, *, defer_content: bool = False):
        super().__init__(parent)
        self._pub_type = pub_type
        self._is_mwb = pub_type == "mwb"
        self._last_pct: int = -1
        self._progress_style_set = False
        self._style_active = False
        self.setObjectName("PubCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(148)
        self._apply_style(active=False)
        self._content_ready = False
        if not defer_content:
            self.build_content()

    def build_content(self) -> None:
        if self._content_ready:
            return
        self._build()
        self._content_ready = True
        self.set_loading()

    def _apply_style(self, active: bool):
        self._style_active = active
        accent = PALETTE.accent if self._is_mwb else MEETING_PURPLE
        border = accent if active else PALETTE.border_muted
        self.setStyleSheet(
            f"QFrame#PubCard{{background:{PALETTE.surface};"
            f"border:1px solid {border};border-radius:16px;}}"
            f"QFrame#PubCard:hover{{background:{PALETTE.surface_hover_strong};"
            f"border-color:{accent};}}"
        )

    def apply_theme(self) -> None:
        self._apply_style(self._style_active)
        if not self._content_ready:
            return
        self._cover.setStyleSheet(
            f"background:{PALETTE.media_placeholder};"
            f"border-radius:8px;border:1px solid {PALETTE.border_muted};"
        )
        pill_color = PALETTE.accent if self._is_mwb else MEETING_PURPLE
        self._pill.setStyleSheet(
            f"background:transparent;color:{pill_color};"
            "font-size:9px;font-weight:800;letter-spacing:1px;"
        )
        self._title_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:15px;font-weight:700;background:transparent;"
        )
        self._sub_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:11px;background:transparent;"
        )

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 16, 14)
        outer.setSpacing(14)

        self._cover = QLabel()
        self._cover.setFixedSize(self._CW, self._CH)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet(
            f"background:{PALETTE.media_placeholder};"
            f"border-radius:8px;border:1px solid {PALETTE.border_muted};"
        )
        outer.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        right = QWidget()
        right.setStyleSheet("background:transparent;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 2, 0, 2)
        rl.setSpacing(0)

        pill_color = PALETTE.accent if self._is_mwb else MEETING_PURPLE
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
            f"color:{PALETTE.text_primary};font-size:15px;font-weight:700;background:transparent;"
        )
        self._title_lbl.setMaximumWidth(260)
        rl.addWidget(self._title_lbl)
        rl.addSpacing(4)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setWordWrap(True)
        self._sub_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:11px;background:transparent;"
        )
        self._sub_lbl.setMaximumWidth(260)
        rl.addWidget(self._sub_lbl)

        rl.addStretch(1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(4)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
        )
        arrow_col = PALETTE.accent if self._is_mwb else MEETING_PURPLE
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
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Loading…"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Fetching publication…"))
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
        )
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)

    def set_ready(self, wd: "WeekData"):
        self._last_pct = -1
        self._progress_style_set = False
        self.setEnabled(True)
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
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

        if cover_bytes:
            pix = QPixmap()
            pix.loadFromData(cover_bytes)
            if not pix.isNull():
                self._cover.setPixmap(rounded_meeting_pixmap(pix, self._CW, self._CH, 8))
                return
        ph = QPixmap(self._CW, self._CH)
        ph.fill(QColor(PALETTE.media_placeholder))
        self._cover.setPixmap(ph)

    def set_saved(self, snapshot: "MeetingTreeSnapshot"):
        self._last_pct = -1
        self._progress_style_set = False
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        fallback_title = self.tr("Life & Ministry") if self._is_mwb else self.tr("Watchtower Study")
        self._title_lbl.setText(snapshot.overview.title or fallback_title)
        self._sub_lbl.setText("")
        n_items = snapshot.media_count
        word = self.tr("item") if n_items == 1 else self.tr("items")
        self._status_lbl.setText(f"{n_items} media {word}")
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

        if snapshot.overview.cover_bytes:
            pix = QPixmap()
            pix.loadFromData(snapshot.overview.cover_bytes)
            if not pix.isNull():
                self._cover.setPixmap(rounded_meeting_pixmap(pix, self._CW, self._CH, 8))
                return
        ph = QPixmap(self._CW, self._CH)
        ph.fill(QColor(PALETTE.media_placeholder))
        self._cover.setPixmap(ph)

    def set_empty(self):
        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("No meeting this week"))
        self._sub_lbl.setText("")
        self._status_lbl.setText("")
        self._cover.setPixmap(QPixmap())
        self._arrow.setVisible(False)
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_error(self, _msg: str = ""):
        self._last_pct = -1
        self._progress_style_set = False
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Unavailable"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.danger};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

    def set_not_found(self):
        self._last_pct = -1
        self._progress_style_set = False
        self.setEnabled(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        pill_text = self.tr("LIFE & MINISTRY") if self._is_mwb else self.tr("WATCHTOWER STUDY")
        self._pill.setText(pill_text)

        self._title_lbl.setText(self.tr("Not available this week"))
        self._sub_lbl.setText("")
        self._status_lbl.setText(self.tr("Tap to retry"))
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.warning};font-size:10px;background:transparent;"
        )
        self._arrow.setVisible(True)

    def set_progress(self, pct: int):
        if pct == self._last_pct:
            return
        self._last_pct = pct

        if not self._progress_style_set:
            self._status_lbl.setStyleSheet(
                f"color:{PALETTE.accent};font-size:10px;background:transparent;"
            )
            self._arrow.setVisible(False)
            self._progress_style_set = True

        self._status_lbl.setText(self.tr("Downloading… {}%").format(pct))


class _MemorialCard(QFrame):
    open_requested = Signal()

    _CW, _CH = 80, 108

    def __init__(self, parent=None, *, defer_content: bool = False):
        super().__init__(parent)
        self._last_pct: int = -1
        self._progress_style_set = False
        self._style_active = False
        self.setObjectName("MemCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(148)
        self._apply_style(active=False)
        self._content_ready = False
        if not defer_content:
            self.build_content()

    def build_content(self) -> None:
        if self._content_ready:
            return
        self._build()
        self._content_ready = True
        self.set_loading()

    def _apply_style(self, active: bool):
        self._style_active = active
        border = PALETTE.warning if active else PALETTE.border_muted
        self.setStyleSheet(
            f"QFrame#MemCard{{background:{PALETTE.surface};"
            f"border:1px solid {border};border-radius:16px;}}"
            f"QFrame#MemCard:hover{{background:{PALETTE.surface_hover_strong};"
            f"border-color:{PALETTE.warning};}}"
        )

    def apply_theme(self) -> None:
        self._apply_style(self._style_active)
        if not self._content_ready:
            return
        self._cover.setStyleSheet(
            f"background:{PALETTE.media_placeholder};"
            f"border-radius:8px;border:1px solid {PALETTE.border_muted};"
        )
        self._pill.setStyleSheet(
            f"background:transparent;color:{PALETTE.warning};"
            "font-size:9px;font-weight:800;letter-spacing:1px;"
        )
        self._title_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:15px;font-weight:700;background:transparent;"
        )
        self._sub_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:11px;background:transparent;"
        )

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 14, 16, 14)
        outer.setSpacing(14)

        self._cover = QLabel()
        self._cover.setFixedSize(self._CW, self._CH)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet(
            f"background:{PALETTE.media_placeholder};"
            f"border-radius:8px;border:1px solid {PALETTE.border_muted};"
        )
        outer.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        right = QWidget()
        right.setStyleSheet("background:transparent;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 2, 0, 2)
        rl.setSpacing(0)

        self._pill = QLabel("")
        self._pill.setStyleSheet(
            f"background:transparent;color:{PALETTE.warning};"
            "font-size:9px;font-weight:800;letter-spacing:1px;"
        )
        rl.addWidget(self._pill)
        rl.addSpacing(6)

        self._title_lbl = QLabel("")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:15px;font-weight:700;background:transparent;"
        )
        self._title_lbl.setMaximumWidth(260)
        rl.addWidget(self._title_lbl)
        rl.addSpacing(4)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setWordWrap(True)
        self._sub_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:11px;background:transparent;"
        )
        self._sub_lbl.setMaximumWidth(260)
        rl.addWidget(self._sub_lbl)

        rl.addStretch(1)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(4)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
        )
        self._arrow = QLabel()
        self._arrow.setPixmap(
            make_icon(ICON_CHEVRON_RIGHT, 13, PALETTE.warning).pixmap(13, 13)
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
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
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
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
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
        ph.fill(QColor(PALETTE.media_placeholder))
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
            f"color:{PALETTE.danger};font-size:10px;background:transparent;"
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
            f"color:{PALETTE.warning};font-size:10px;background:transparent;"
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
            f"color:{PALETTE.text_muted};font-size:10px;background:transparent;"
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
                f"color:{PALETTE.warning};font-size:10px;background:transparent;"
            )
            self._arrow.setVisible(False)
            self._progress_style_set = True
        self._status_lbl.setText(self.tr("Downloading… {}%").format(pct))

    def set_not_yet(self):
        self.setVisible(False)


class Overview(QWidget):
    open_mwb = Signal()
    open_wt = Signal()
    open_memorial = Signal()

    def __init__(self, parent=None, *, defer_cards: bool = False):
        super().__init__(parent)
        self._scroll: QScrollArea | None = None
        self._content: QWidget | None = None
        self.apply_theme()
        self._begin_build()
        if not defer_cards:
            self.install_content()
            self.build_mwb_card()
            self.populate_mwb_card()
            self.build_wt_card()
            self.populate_wt_card()
            self.build_memorial_card()
            self.populate_memorial_card()
            self.finish_build()

    def _begin_build(self):
        scroll = QScrollArea()
        self._scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._content = content
        cl = QVBoxLayout(content)
        cl.setContentsMargins(16, 12, 16, 24)
        cl.setSpacing(12)
        self._content_layout = cl

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)
        self.apply_theme()

    def install_content(self) -> None:
        if self._scroll is not None and self._content is not None:
            self._scroll.setWidget(self._content)

    def build_mwb_card(self) -> None:
        self.mwb_card = _PubCard("mwb", defer_content=True)
        self.mwb_card.open_requested.connect(self.open_mwb)
        self._content_layout.addWidget(self.mwb_card)

    def populate_mwb_card(self) -> None:
        self.mwb_card.build_content()

    def build_wt_card(self) -> None:
        self.wt_card = _PubCard("wt", defer_content=True)
        self.wt_card.open_requested.connect(self.open_wt)
        self._content_layout.addWidget(self.wt_card)

    def populate_wt_card(self) -> None:
        self.wt_card.build_content()

    def build_memorial_card(self) -> None:
        self.memorial_card = _MemorialCard(defer_content=True)
        self.memorial_card.open_requested.connect(self.open_memorial)
        self.memorial_card.setVisible(False)
        self._content_layout.addWidget(self.memorial_card)

    def populate_memorial_card(self) -> None:
        self.memorial_card.build_content()

    def finish_build(self) -> None:
        self._content_layout.addStretch()

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background:{PALETTE.bg0};")
        if self._scroll is not None:
            self._scroll.setStyleSheet(
                f"QScrollArea{{border:none;background:{PALETTE.bg0};}}"
                "QScrollBar:vertical{width:4px;background:transparent;}"
                f"QScrollBar::handle:vertical{{background:{PALETTE.border};"
                "border-radius:2px;min-height:20px;}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            )
        if self._content is not None:
            self._content.setStyleSheet(f"background:{PALETTE.bg0};")
        for card in (
            getattr(self, "mwb_card", None),
            getattr(self, "wt_card", None),
            getattr(self, "memorial_card", None),
        ):
            if card is not None:
                card.apply_theme()
