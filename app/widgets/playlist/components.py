from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
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

_MENU_STYLE = (
    "QMenu{background:#161b22;border:1px solid #30363d;border-radius:6px;"
    "padding:4px;color:#c9d1d9;font-size:11px;}"
    "QMenu::item{padding:7px 18px;border-radius:4px;}"
    "QMenu::item:selected{background:#1f3a5f;color:#79c0ff;}"
    "QMenu::separator{height:1px;background:#21262d;margin:3px 8px;}"
)


class _PlaylistCard(QFrame):
    clicked = Signal(str)
    rename_req = Signal(str)
    delete_req = Signal(str)
    export_req = Signal(str)

    def __init__(self, playlist_id: str, name: str, count: int, lang, parent=None):
        super().__init__(parent)
        self._id = playlist_id
        self._lang = lang
        self.setObjectName("PCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._set_style(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(make_icon(ICON_NAV_PLAYLIST, 20, "#388bfd").pixmap(20, 20))
        icon_lbl.setStyleSheet("background:#1a2744;border-radius:8px;")

        txt = QVBoxLayout()
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        self.name_lbl = QLabel(name)
        self.name_lbl.setStyleSheet(
            "color:#e6edf3;font-size:12px;font-weight:600;background:transparent;"
        )
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.name_lbl.setMinimumWidth(0)
        self.name_lbl.setWordWrap(False)
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl = QLabel(self._count_str(count))
        self.count_lbl.setStyleSheet("color:#484f58;font-size:10px;background:transparent;")
        txt.addWidget(self.name_lbl)
        txt.addWidget(self.count_lbl)

        self._mbtn = QPushButton()
        self._mbtn.setFixedSize(24, 24)
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, "#484f58"))
        self._mbtn.setIconSize(QSize(13, 13))
        self._mbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mbtn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:4px;}"
            "QPushButton:hover{background:#21262d;}"
        )
        self._mbtn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mbtn.clicked.connect(self._show_menu)

        lay.addWidget(icon_lbl)
        lay.addLayout(txt, stretch=1)
        lay.addWidget(self._mbtn, alignment=Qt.AlignmentFlag.AlignVCenter)

    def _set_style(self, hovered: bool) -> None:
        if hovered:
            self.setStyleSheet(
                "QFrame#PCard{background:#1c2128;border-radius:8px;border:1px solid #388bfd;}"
                "QLabel{background:transparent;}"
            )
        else:
            self.setStyleSheet(
                "QFrame#PCard{background:#13161c;border-radius:8px;border:1px solid #21262d;}"
                "QLabel{background:transparent;}"
            )

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
        menu.setStyleSheet(_MENU_STYLE)
        ar = QAction(self)
        ar.setIcon(make_icon(ICON_EDIT, 13, "#c9d1d9"))
        ar.setText("  " + self.tr("Rename"))
        ar.triggered.connect(lambda: self.rename_req.emit(self._id))
        ae = QAction(self)
        ae.setIcon(make_icon(ICON_EXPORT, 13, "#c9d1d9"))
        ae.setText("  " + self.tr("Export .jwlplaylist"))
        ae.triggered.connect(lambda: self.export_req.emit(self._id))
        ad = QAction(self)
        ad.setIcon(make_icon(ICON_TRASH, 13, "#f85149"))
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


class _Toast(QLabel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "QLabel{background:#1f3a5f;color:#79c0ff;font-size:11px;font-weight:600;"
            "border:1px solid #388bfd;border-radius:8px;padding:7px 16px;}"
        )
        self.setVisible(False)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(1.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade)
        self._anim.finished.connect(self._done)
        self._fading = False

    def show_message(self, msg: str, duration: int = 2400) -> None:
        self._fading = False
        self._anim.stop()
        self.setText(msg)
        self.adjustSize()
        self.setMinimumWidth(200)
        self._repos()
        self._fx.setOpacity(1.0)
        self.raise_()
        self.setVisible(True)
        self._timer.start(duration)

    def _repos(self) -> None:
        p = self.parent()
        if p:
            self.move((p.width() - self.width()) // 2, p.height() - self.height() - 68)

    def resizeEvent(self, e) -> None:
        self._repos()
        super().resizeEvent(e)

    def _fade(self) -> None:
        self._fading = True
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(450)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

    def _done(self) -> None:
        if self._fading:
            self.setVisible(False)
            self._fx.setOpacity(1.0)
            self._fading = False


class _CollapsibleSection(QWidget):
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
            "QPushButton:hover{background:#21262d;}"
        )
        self._toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._toggle_btn.clicked.connect(self._on_toggle)

        self._title_lbl = QLabel(title.upper(), self)
        self._title_lbl.setStyleSheet(
            "color:#8b949e;font-size:10px;font-weight:700;background:transparent;"
        )

        self._count_lbl = QLabel(parent=self)
        self._count_lbl.setStyleSheet("color:#484f58;font-size:10px;background:transparent;")

        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#21262d;background:#21262d;border:none;max-height:1px;")

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
        self._toggle_btn.setIcon(make_icon(arrow, 11, "#484f58"))
        self._toggle_btn.setIconSize(QSize(11, 11))


class _WatchedFolderCard(QFrame):
    clicked = Signal(str)
    rename_req = Signal(str)
    delete_req = Signal(str)
    export_req = Signal(str)

    _AMBER = "#e3a436"
    _AMBER_BG = "#2a1f0a"

    def __init__(self, path: str, name: str, count: int, lang, parent=None):
        super().__init__(parent)
        self._path = path
        self._lang = lang
        self.setObjectName("PCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._set_style(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setPixmap(make_icon(ICON_FOLDER_LINK, 20, self._AMBER).pixmap(20, 20))
        icon_lbl.setStyleSheet(f"background:{self._AMBER_BG};border-radius:8px;")

        txt = QVBoxLayout()
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        self.name_lbl = QLabel(name)
        self.name_lbl.setStyleSheet(
            "color:#e6edf3;font-size:12px;font-weight:600;background:transparent;"
        )
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.name_lbl.setWordWrap(False)
        self.name_lbl.setText(
            self.name_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 220)
        )
        self.name_lbl.setToolTip(name)
        self.count_lbl = QLabel(self._count_str(count))
        self.count_lbl.setStyleSheet("color:#484f58;font-size:10px;background:transparent;")
        txt.addWidget(self.name_lbl)
        txt.addWidget(self.count_lbl)

        self._mbtn = QPushButton()
        self._mbtn.setFixedSize(24, 24)
        self._mbtn.setIcon(make_icon(ICON_MORE_VERT, 13, "#484f58"))
        self._mbtn.setIconSize(QSize(13, 13))
        self._mbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mbtn.setStyleSheet(
            "QPushButton{border:none;background:transparent;border-radius:4px;}"
            "QPushButton:hover{background:#21262d;}"
        )
        self._mbtn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mbtn.clicked.connect(self._show_menu)

        lay.addWidget(icon_lbl)
        lay.addLayout(txt, stretch=1)
        lay.addWidget(self._mbtn, alignment=Qt.AlignmentFlag.AlignVCenter)

    def _set_style(self, hovered: bool) -> None:
        border = self._AMBER if hovered else "#2d1e08"
        bg = "#1c1810" if hovered else "#13120e"
        self.setStyleSheet(
            f"QFrame#PCard{{background:{bg};border-radius:8px;border:1px solid {border};}}"
            "QLabel{background:transparent;}"
        )

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
        menu.setStyleSheet(_MENU_STYLE)

        ar = QAction(self)
        ar.setIcon(make_icon(ICON_EDIT, 13, "#c9d1d9"))
        ar.setText("  " + self.tr("Rename"))
        ar.triggered.connect(lambda: self.rename_req.emit(self._path))

        ae = QAction(self)
        ae.setIcon(make_icon(ICON_EXPORT, 13, "#c9d1d9"))
        ae.setText("  " + self.tr("Export .jwlplaylist"))
        ae.triggered.connect(lambda: self.export_req.emit(self._path))

        ad = QAction(self)
        ad.setIcon(make_icon(ICON_TRASH, 13, "#f85149"))
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
