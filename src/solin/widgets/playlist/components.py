from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...styles.icons import (
    ICON_EDIT,
    ICON_EXPORT,
    ICON_FOLDER_LINK,
    ICON_MORE_VERT,
    ICON_NAV_PLAYLIST,
    ICON_TRASH,
    make_icon,
)
from ...styles.theme import PALETTE, qss_rgba


def playlist_card_menu_stylesheet() -> str:
    return (
        f"QMenu{{background:{PALETTE.surface};border:1px solid {PALETTE.border};"
        f"border-radius:6px;padding:4px;color:{PALETTE.text_secondary};"
        "font-size:11px;}}"
        "QMenu::item{padding:7px 18px;border-radius:4px;}"
        f"QMenu::item:selected{{background:{PALETTE.accent_muted};"
        f"color:{PALETTE.accent_text};}}"
        f"QMenu::separator{{height:1px;background:{PALETTE.border_muted};"
        "margin:3px 8px;}}"
    )


PLAYLIST_CARD_MENU_STYLESHEET = playlist_card_menu_stylesheet()

__all__ = (
    "CollapsibleSection",
    "PLAYLIST_CARD_MENU_STYLESHEET",
    "PlaylistCard",
    "WatchedFolderCard",
)


class PlaylistCard(QFrame):
    clicked = Signal(str)
    rename_req = Signal(str)
    delete_req = Signal(str)
    export_req = Signal(str)

    def __init__(self, playlist_id: str, name: str, count: int, lang, parent=None):
        super().__init__(parent)
        self._id = playlist_id
        self._lang = lang
        self._hovered = False
        self.setObjectName("PCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._set_style(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(10)

        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(36, 36)
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        txt = QVBoxLayout()
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        self.name_lbl = QLabel(name)
        self.name_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:12px;font-weight:600;background:transparent;"
        )
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.name_lbl.setMinimumWidth(0)
        self.name_lbl.setWordWrap(False)
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl = QLabel(self._count_str(count))
        self.count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )
        txt.addWidget(self.name_lbl)
        txt.addWidget(self.count_lbl)

        self._mbtn = QPushButton()
        self._mbtn.setFixedSize(24, 24)
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, PALETTE.text_dim))
        self._mbtn.setIconSize(QSize(13, 13))
        self._mbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mbtn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mbtn.clicked.connect(self._show_menu)

        lay.addWidget(self.icon_lbl)
        lay.addLayout(txt, stretch=1)
        lay.addWidget(self._mbtn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.apply_theme()

    def _set_style(self, hovered: bool) -> None:
        self._hovered = hovered
        if hovered:
            self.setStyleSheet(
                f"QFrame#PCard{{background:{PALETTE.surface_hover_strong};"
                f"border-radius:8px;border:1px solid {PALETTE.accent};}}"
                "QLabel{background:transparent;}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#PCard{{background:{PALETTE.surface_card};"
                f"border-radius:8px;border:1px solid {PALETTE.border_muted};}}"
                "QLabel{background:transparent;}"
            )

    def apply_theme(self) -> None:
        self.icon_lbl.setPixmap(make_icon(ICON_NAV_PLAYLIST, 20, PALETTE.accent).pixmap(20, 20))
        self.icon_lbl.setStyleSheet(f"background:{PALETTE.accent_tint};border-radius:8px;")
        self.name_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:12px;font-weight:600;background:transparent;"
        )
        self.count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, PALETTE.text_dim))
        self._mbtn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:4px;}"
            f"QPushButton:hover{{background:{PALETTE.bg2};}}"
        )
        self._set_style(self._hovered)

    def _count_str(self, count: int) -> str:
        word = self.tr("item") if count == 1 else self.tr("items")
        return f"{count} {word}"

    def update_info(self, name: str, count: int) -> None:
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl.setText(self._count_str(count))

    def enterEvent(self, e) -> None:
        self._set_style(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._set_style(False)
        super().leaveEvent(e)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(playlist_card_menu_stylesheet())
        ar = QAction(self)
        ar.setIcon(make_icon(ICON_EDIT, 13, PALETTE.text_secondary))
        ar.setText("  " + self.tr("Rename"))
        ar.triggered.connect(lambda: self.rename_req.emit(self._id))
        ae = QAction(self)
        ae.setIcon(make_icon(ICON_EXPORT, 13, PALETTE.text_secondary))
        ae.setText("  " + self.tr("Export .jwlplaylist"))
        ae.triggered.connect(lambda: self.export_req.emit(self._id))
        ad = QAction(self)
        ad.setIcon(make_icon(ICON_TRASH, 13, PALETTE.danger))
        ad.setText("  " + self.tr("Delete"))
        ad.triggered.connect(lambda: self.delete_req.emit(self._id))
        menu.addAction(ar)
        menu.addAction(ae)
        menu.addSeparator()
        menu.addAction(ad)
        menu.exec(self._mbtn.mapToGlobal(QPoint(0, self._mbtn.height())))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._mbtn.geometry().contains(event.position().toPoint()):
                self.clicked.emit(self._id)
        super().mousePressEvent(event)


class CollapsibleSection(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._build_ui(title)

    def _build_ui(self, title: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(8)

        self._toggle_btn = QPushButton(self)
        self._toggle_btn.setFixedSize(20, 20)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:3px;}"
            f"QPushButton:hover{{background:{PALETTE.bg2};}}"
        )
        self._toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._toggle_btn.clicked.connect(self._on_toggle)

        self._title_lbl = QLabel(title.upper(), self)
        self._title_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;font-weight:700;background:transparent;"
        )

        self._count_lbl = QLabel(parent=self)
        self._count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )

        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(
            f"color:{PALETTE.border_muted};background:{PALETTE.border_muted};"
            "border:none;max-height:1px;"
        )
        self._sep = sep

        hdr.addWidget(self._toggle_btn)
        hdr.addWidget(self._title_lbl)
        hdr.addWidget(self._count_lbl)
        hdr.addWidget(sep, stretch=1)
        self._update_chevron()

        self._header_widget = QWidget(self)
        self._header_widget.setLayout(hdr)
        root.addWidget(self._header_widget)

        self._content: QWidget | None = None

    def set_header_visible(self, visible: bool) -> None:
        self._header_widget.setVisible(visible)

    def set_content(self, widget: QWidget) -> None:
        self._content = widget
        self.layout().addWidget(widget)

    def set_count(self, n: int) -> None:
        self._count_lbl.setText(f"({n})" if n else "")

    def set_title(self, text: str) -> None:
        self._title_lbl.setText(text)

    def apply_theme(self) -> None:
        self._toggle_btn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:3px;}"
            f"QPushButton:hover{{background:{PALETTE.bg2};}}"
        )
        self._title_lbl.setStyleSheet(
            f"color:{PALETTE.text_muted};font-size:10px;font-weight:700;background:transparent;"
        )
        self._count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )
        self._sep.setStyleSheet(
            f"color:{PALETTE.border_muted};background:{PALETTE.border_muted};"
            "border:none;max-height:1px;"
        )
        self._update_chevron()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _on_toggle(self) -> None:
        self._collapsed = not self._collapsed
        if self._content:
            self._content.setVisible(not self._collapsed)
        self._update_chevron()

    def _update_chevron(self) -> None:
        arrow = (
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="9,18 15,12 9,6"/></svg>'
            if self._collapsed else
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="6,9 12,15 18,9"/></svg>'
        )
        self._toggle_btn.setIcon(make_icon(arrow, 11, PALETTE.text_dim))
        self._toggle_btn.setIconSize(QSize(11, 11))


class WatchedFolderCard(QFrame):
    clicked = Signal(str)
    rename_req = Signal(str)
    delete_req = Signal(str)
    export_req = Signal(str)

    def __init__(self, path: str, name: str, count: int, lang, parent=None):
        super().__init__(parent)
        self._path = path
        self._lang = lang
        self._hovered = False
        self.setObjectName("PCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._set_style(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(10)

        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(36, 36)
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        txt = QVBoxLayout()
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        self.name_lbl = QLabel(name)
        self.name_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:12px;font-weight:600;background:transparent;"
        )
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.name_lbl.setWordWrap(False)
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl = QLabel(self._count_str(count))
        self.count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )
        txt.addWidget(self.name_lbl)
        txt.addWidget(self.count_lbl)

        self._mbtn = QPushButton()
        self._mbtn.setFixedSize(24, 24)
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, PALETTE.text_dim))
        self._mbtn.setIconSize(QSize(13, 13))
        self._mbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mbtn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mbtn.clicked.connect(self._show_menu)

        lay.addWidget(self.icon_lbl)
        lay.addLayout(txt, stretch=1)
        lay.addWidget(self._mbtn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.apply_theme()

    def _set_style(self, hovered: bool) -> None:
        self._hovered = hovered
        border = PALETTE.warning if hovered else qss_rgba(PALETTE.warning, 0.28)
        bg = qss_rgba(PALETTE.warning, 0.12 if hovered else 0.07)
        self.setStyleSheet(
            f"QFrame#PCard{{background:{bg};border-radius:8px;border:1px solid {border};}}"
            "QLabel{background:transparent;}"
        )

    def apply_theme(self) -> None:
        self.icon_lbl.setPixmap(make_icon(ICON_FOLDER_LINK, 20, PALETTE.warning).pixmap(20, 20))
        self.icon_lbl.setStyleSheet(
            f"background:{qss_rgba(PALETTE.warning, 0.14)};border-radius:8px;"
        )
        self.name_lbl.setStyleSheet(
            f"color:{PALETTE.text_primary};font-size:12px;font-weight:600;background:transparent;"
        )
        self.count_lbl.setStyleSheet(
            f"color:{PALETTE.text_dim};font-size:10px;background:transparent;"
        )
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, PALETTE.text_dim))
        self._mbtn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:4px;}"
            f"QPushButton:hover{{background:{PALETTE.bg2};}}"
        )
        self._set_style(self._hovered)

    def _count_str(self, count: int) -> str:
        word = self.tr("item") if count == 1 else self.tr("items")
        return f"{count} {word}"

    def update_info(self, name: str, count: int) -> None:
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl.setText(self._count_str(count))

    def enterEvent(self, e) -> None:
        self._set_style(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._set_style(False)
        super().leaveEvent(e)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(playlist_card_menu_stylesheet())

        ar = QAction(self)
        ar.setIcon(make_icon(ICON_EDIT, 13, PALETTE.text_secondary))
        ar.setText("  " + self.tr("Rename"))
        ar.triggered.connect(lambda: self.rename_req.emit(self._path))

        ae = QAction(self)
        ae.setIcon(make_icon(ICON_EXPORT, 13, PALETTE.text_secondary))
        ae.setText("  " + self.tr("Export .jwlplaylist"))
        ae.triggered.connect(lambda: self.export_req.emit(self._path))

        ad = QAction(self)
        ad.setIcon(make_icon(ICON_TRASH, 13, PALETTE.danger))
        ad.setText("  " + self.tr("Delete"))
        ad.triggered.connect(lambda: self.delete_req.emit(self._path))

        menu.addAction(ar)
        menu.addAction(ae)
        menu.addSeparator()
        menu.addAction(ad)
        menu.exec(self._mbtn.mapToGlobal(QPoint(0, self._mbtn.height())))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._mbtn.geometry().contains(event.position().toPoint()):
                self.clicked.emit(self._path)
        super().mousePressEvent(event)
