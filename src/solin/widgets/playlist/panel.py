"""Sliding playlist panel used by the main window."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from solin.core.ui.helpers import make_rounded_thumb
from solin.styles.icons import ICON_IMAGE, ICON_MUSIC, ICON_VIDEO, make_icon
from .items import media_type_from_url


_PANEL_W = 210
_THUMB_W = 68
_THUMB_H = 40
_ANIM_MS = 220


class _PlaylistItem(QFrame):
    """Single playlist row with thumbnail, title and media type."""

    clicked_index = Signal(int)

    _CSS_NORMAL = (
        "QFrame{background:#13161c;border-radius:6px;border:1px solid #21262d;outline:none;}"
        "QFrame:hover{background:#1c2128;border-color:#388bfd;outline:none;}"
        "QLabel{outline:none;border:none;background:transparent;}"
    )
    _CSS_ACTIVE = (
        "QFrame{background:#1c2128;border-radius:6px;border:1.5px solid #388bfd;outline:none;}"
        "QLabel{outline:none;border:none;background:transparent;}"
    )

    def __init__(self, index: int, title: str, media_type: str, type_label: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._media_type = media_type
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 8, 6)
        layout.setSpacing(8)

        self.thumb = QLabel()
        self.thumb.setFixedSize(_THUMB_W, _THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            "background:#0d1117;border-radius:4px;border:1px solid #30363d;"
        )
        icon_svg = ICON_IMAGE if media_type == "image" else (
            ICON_MUSIC if media_type == "audio" else ICON_VIDEO
        )
        self.thumb.setPixmap(make_icon(icon_svg, 18, "#484f58").pixmap(18, 18))

        info = QWidget()
        info.setStyleSheet("background:transparent;")
        info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 1, 0, 1)
        info_layout.setSpacing(3)

        self.title_lbl = QLabel(title)
        self.title_lbl.setWordWrap(False)
        self.title_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.title_lbl.setStyleSheet(
            "background:transparent;color:#c9d1d9;font-size:10px;font-weight:500;"
        )
        self.title_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.title_lbl.setMaximumWidth(200)
        elided = self.title_lbl.fontMetrics().elidedText(
            title,
            Qt.TextElideMode.ElideRight,
            120,
        )
        self.title_lbl.setText(elided)
        self.title_lbl.setToolTip(title)

        self.type_lbl = QLabel(type_label)
        self.type_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.type_lbl.setStyleSheet(
            "background:transparent;color:#484f58;font-size:9px;"
        )

        info_layout.addWidget(self.title_lbl)
        info_layout.addWidget(self.type_lbl)
        info_layout.addStretch()

        layout.addWidget(self.thumb)
        layout.addWidget(info, stretch=1)
        self.setStyleSheet(self._CSS_NORMAL)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumb.setPixmap(make_rounded_thumb(pixmap))

    def refresh_language_label(self, type_label: str) -> None:
        self.type_lbl.setText(type_label)

    def set_active(self, active: bool) -> None:
        self.setStyleSheet(self._CSS_ACTIVE if active else self._CSS_NORMAL)
        color = "#e6edf3" if active else "#c9d1d9"
        weight = "700" if active else "500"
        self.title_lbl.setStyleSheet(
            f"background:transparent;color:{color};"
            f"font-size:10px;font-weight:{weight};outline:none;border:none;"
        )
        self.type_lbl.setStyleSheet(
            "background:transparent;color:#484f58;font-size:9px;outline:none;border:none;"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked_index.emit(self._index)
        super().mousePressEvent(event)


class PlaylistPanel(QWidget):
    """Sliding side panel with thumbnails for the active playlist."""

    item_clicked = Signal(int)

    def __init__(self, lang=None, parent=None):
        super().__init__(parent)
        self._lang = lang
        self._items: list[_PlaylistItem] = []
        self._anim: QPropertyAnimation | None = None
        self._open = False

        self.setMaximumWidth(0)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background:#0d1117;border-left:1px solid #21262d;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet("background:#161b22;border-bottom:1px solid #30363d;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        self._count_lbl = QLabel("Playlist")
        self._count_lbl.setStyleSheet(
            "background:transparent;color:#8b949e;"
            "font-size:10px;font-weight:600;letter-spacing:0.5px;"
        )
        header_layout.addWidget(self._count_lbl)
        header_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{width:4px;background:#0d1117;border-radius:2px;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:2px;"
            "min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        self._items_w = QWidget()
        self._items_w.setStyleSheet("background:transparent;")
        self._items_lay = QVBoxLayout(self._items_w)
        self._items_lay.setContentsMargins(6, 6, 6, 6)
        self._items_lay.setSpacing(4)
        self._items_lay.addStretch()
        scroll.setWidget(self._items_w)
        self._scroll = scroll

        outer.addWidget(header)
        outer.addWidget(scroll, stretch=1)

    def _get_panel_width(self) -> int:
        return self.maximumWidth()

    def _set_panel_width(self, width: int) -> None:
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)

    panelWidth = Property(int, _get_panel_width, _set_panel_width)

    def populate(self, items: list, current_index: int) -> None:
        while self._items_lay.count():
            item = self._items_lay.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        self._items.clear()

        self._update_count_label(len(items))

        for index, data in enumerate(items):
            url = data.get("url", "")
            media_type = data.get("type") or media_type_from_url(url)
            title = data.get("title", f"Item {index + 1}")
            row = _PlaylistItem(index, title, media_type, self._get_type_label(media_type))
            row.clicked_index.connect(self.item_clicked)
            self._items.append(row)
            self._items_lay.addWidget(row)

        self._items_lay.addStretch()
        self._highlight(current_index)

    def set_thumbnail(self, index: int, pixmap: QPixmap) -> None:
        if 0 <= index < len(self._items):
            self._items[index].set_thumbnail(pixmap)

    def set_active(self, index: int) -> None:
        self._highlight(index)
        if 0 <= index < len(self._items):
            QTimer.singleShot(
                30,
                lambda: self._scroll.ensureWidgetVisible(self._items[index], 0, 16),
            )

    def _get_type_label(self, media_type: str) -> str:
        if media_type == "image":
            return self.tr("Image")
        if media_type == "audio":
            return self.tr("Audio")
        return self.tr("Video")

    def _update_count_label(self, count: int) -> None:
        self._count_lbl.setText(self.tr("%n media item(s)", "", count))

    def refresh_language(self, lang=None) -> None:
        if lang:
            self._lang = lang
        self._update_count_label(len(self._items))
        for item in self._items:
            item.refresh_language_label(self._get_type_label(item._media_type))

    def toggle(self) -> None:
        self.close_panel() if self._open else self.open_panel()

    def open_panel(self) -> None:
        self._open = True
        self._animate_to(_PANEL_W)

    def close_panel(self) -> None:
        self._open = False
        self._animate_to(0)

    def is_open(self) -> bool:
        return self._open

    def _highlight(self, index: int) -> None:
        for row_index, widget in enumerate(self._items):
            widget.set_active(row_index == index)

    def _animate_to(self, target: int) -> None:
        if self._anim and self._anim.state() == QPropertyAnimation.State.Running:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"panelWidth", self)
        self._anim.setDuration(_ANIM_MS)
        self._anim.setStartValue(self._get_panel_width())
        self._anim.setEndValue(target)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.start()
