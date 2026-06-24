from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
)

from ...styles.icons import ICON_ASPECT_MATCH, ICON_CAST, ICON_CROP, make_icon
from ...styles.theme import PALETTE
from .tab_bar import BrowserTabBar


__all__ = ("BrowserUiMixin",)


class BrowserUiMixin:
    @staticmethod
    def _std_icon(sp) -> QIcon:
        return QApplication.style().standardIcon(sp)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("Card")
        topbar.setFixedHeight(52)
        topbar.setCursor(Qt.CursorShape.ArrowCursor)
        topbar.setStyleSheet(
            "QFrame#Card { border-radius:0; border-left:none;"
            " border-right:none; border-top:none; }"
        )
        nav = QHBoxLayout(topbar)
        nav.setContentsMargins(10, 0, 12, 0)
        nav.setSpacing(4)

        btn_style = (
            "QPushButton {"
            f"  padding:0; border:1px solid {PALETTE.border}; border-radius:6px;"
            f"  background:{PALETTE.bg2}; min-width:0; }}"
            f"QPushButton:hover:enabled  {{ background:{PALETTE.bg3};"
            f" border-color:{PALETTE.text_muted}; }}"
            f"QPushButton:pressed:enabled{{ background:{PALETTE.bg1}; }}"
            f"QPushButton:disabled {{ background:{PALETTE.surface_overlay};"
            f" border-color:{PALETTE.border_muted};"
            " opacity:0.38; }"
        )

        self.back_btn = QPushButton()
        self.back_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowBack))
        self.back_btn.setFixedSize(34, 34)
        self.back_btn.setToolTip(self.tr("Back (Alt+←)"))
        self.back_btn.setStyleSheet(btn_style)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.setEnabled(False)
        self.back_btn.clicked.connect(self._go_back)
        nav.addWidget(self.back_btn)

        self.fwd_btn = QPushButton()
        self.fwd_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowForward))
        self.fwd_btn.setFixedSize(34, 34)
        self.fwd_btn.setToolTip(self.tr("Forward (Alt+→)"))
        self.fwd_btn.setStyleSheet(btn_style)
        self.fwd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fwd_btn.setEnabled(False)
        self.fwd_btn.clicked.connect(self._go_forward)
        nav.addWidget(self.fwd_btn)

        self.reload_btn = QPushButton()
        self.reload_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_btn.setFixedSize(34, 34)
        self.reload_btn.setToolTip(self.tr("Reload (F5)  ·  Ctrl+F5: clear cookies & reload"))
        self.reload_btn.setStyleSheet(btn_style)
        self.reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_btn.clicked.connect(self._reload)
        nav.addWidget(self.reload_btn)

        nav.addSpacing(4)

        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("UrlBar")
        self.url_edit.setPlaceholderText(self.tr("Paste or type a URL…"))
        self.url_edit.setCursor(Qt.CursorShape.IBeamCursor)
        self.url_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.url_edit.setFixedHeight(34)
        self.url_edit.setStyleSheet(
            "QLineEdit#UrlBar {"
            f"  border:1px solid {PALETTE.border}; border-radius:6px;"
            "  padding:0 10px; font-size:12px;"
            f"  background:{PALETTE.bg0}; color:{PALETTE.text_secondary};"
            f"  selection-background-color:{PALETTE.accent}; }}"
            f"QLineEdit#UrlBar:focus {{ border-color:{PALETTE.accent}; }}"
        )
        self.url_edit.returnPressed.connect(self._navigate_from_bar)
        nav.addWidget(self.url_edit)
        nav.addSpacing(4)

        self.cast_btn = QPushButton()
        self.cast_btn.setFixedSize(34, 34)
        self.cast_btn.setCheckable(True)
        self.cast_btn.setToolTip(self.tr("Project this tab live"))
        self.cast_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cast_btn_style_off = btn_style
        self._cast_btn_style_on = (
            "QPushButton {"
            "  padding:0; border:1px solid #388bfd; border-radius:6px;"
            "  background:#0d2044; min-width:0; }"
            "QPushButton:hover:enabled  { background:#0a2a5e; border-color:#58a6ff; }"
            "QPushButton:pressed:enabled{ background:#07173a; }"
        )
        self.cast_btn.setIcon(make_icon(ICON_CAST, 16, PALETTE.text_muted))
        self.cast_btn.setStyleSheet(self._cast_btn_style_off)
        self.cast_btn.clicked.connect(self._on_cast_clicked)
        nav.addWidget(self.cast_btn)

        self._crop_btn_style_off = btn_style
        self._crop_btn_style_on = (
            "QPushButton {"
            "  padding:0; border:1px solid #f0883e; border-radius:6px;"
            "  background:#2d1a00; min-width:0; }"
            "QPushButton:hover:enabled  { background:#3d2500; border-color:#ffa657; }"
            "QPushButton:pressed:enabled{ background:#1e1100; }"
        )
        self.crop_btn = QPushButton()
        self.crop_btn.setFixedSize(34, 34)
        self.crop_btn.setCheckable(True)
        self.crop_btn.setToolTip(self.tr("Project region of page"))
        self.crop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.crop_btn.setIcon(make_icon(ICON_CROP, 16, PALETTE.text_muted))
        self.crop_btn.setStyleSheet(self._crop_btn_style_off)
        self.crop_btn.toggled.connect(self._on_crop_toggled)
        nav.addWidget(self.crop_btn)

        self._cursor_btn_style_off = btn_style
        self._cursor_btn_style_on = (
            "QPushButton {"
            "  padding:0; border:1px solid #2dd4bf; border-radius:6px;"
            "  background:#0a2a24; min-width:0; }"
            "QPushButton:hover:enabled  { background:#0d3830; border-color:#5eead4; }"
            "QPushButton:pressed:enabled{ background:#062018; }"
        )
        self.cursor_btn = QPushButton()
        self.cursor_btn.setFixedSize(34, 34)
        self.cursor_btn.setCheckable(True)
        self.cursor_btn.setToolTip(self.tr("Cursor spotlight (presentation mode)"))
        self.cursor_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cursor_btn.setIcon(self._make_spotlight_icon(PALETTE.text_muted))
        self.cursor_btn.setStyleSheet(self._cursor_btn_style_off)
        self.cursor_btn.toggled.connect(self._on_cursor_toggled)
        nav.addWidget(self.cursor_btn)

        self._aspect_btn_style_off = btn_style
        self._aspect_btn_style_on = (
            "QPushButton {"
            "  padding:0; border:1px solid #a371f7; border-radius:6px;"
            "  background:#24143f; min-width:0; }"
            "QPushButton:hover:enabled  { background:#2f1a52; border-color:#bc8cff; }"
            "QPushButton:pressed:enabled{ background:#1b0f30; }"
        )
        self.aspect_btn = QPushButton()
        self.aspect_btn.setFixedSize(34, 34)
        self.aspect_btn.setCheckable(True)
        self.aspect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.aspect_btn.setIcon(make_icon(ICON_ASPECT_MATCH, 16, PALETTE.text_muted))
        self.aspect_btn.setStyleSheet(self._aspect_btn_style_off)
        self.aspect_btn.toggled.connect(self._on_aspect_lock_toggled)
        self._update_aspect_lock_btn_visual(False)
        nav.addWidget(self.aspect_btn)

        root.addWidget(topbar)

        row_h = 36
        btn_h = 26
        btn_w = 26
        gap = 4

        tab_row = QFrame()
        tab_row.setFixedHeight(row_h)
        tab_row.setCursor(Qt.CursorShape.ArrowCursor)
        tab_row.setStyleSheet(f"QFrame {{ background:{PALETTE.surface}; border:none; }}")

        tab_lay = QHBoxLayout(tab_row)
        tab_lay.setContentsMargins(0, 0, gap, 0)
        tab_lay.setSpacing(0)

        self._tab_bar = BrowserTabBar()
        self._tab_bar.setFixedHeight(row_h)
        self._tab_bar.setCursor(Qt.CursorShape.ArrowCursor)
        self._tab_bar.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        self._tab_bar.tabCloseRequested.connect(self._close_tab)
        self._tab_bar.tabMoved.connect(self._on_tab_moved)
        self._tab_bar.setStyleSheet(
            f"QTabBar {{ background:{PALETTE.surface}; border:none; }}"
            "QTabBar::tab {"
            f"  min-height:{row_h - 2}px;"
            f"  background:{PALETTE.surface}; color:{PALETTE.text_muted};"
            "  padding:0 14px; font-size:12px;"
            "  min-width:60px; max-width:180px;"
            f"  border-right:1px solid {PALETTE.border_muted}; }}"
            "QTabBar::tab:selected {"
            f"  color:{PALETTE.text_primary}; border-bottom:2px solid {PALETTE.accent};"
            " font-weight:600; }"
            f"QTabBar::tab:hover {{ background:{PALETTE.bg2}; color:{PALETTE.text_secondary}; }}"
            "QTabBar::close-button { subcontrol-position:right; }"
            "QTabBar QToolButton {"
            f"  width:{btn_w}px; height:{btn_h}px;"
            f"  margin: {(row_h - btn_h)//2}px 2px;"
            f"  background:{PALETTE.surface}; border:none; border-radius:4px; }}"
            f"QTabBar QToolButton:hover   {{ background:{PALETTE.bg3}; }}"
            f"QTabBar QToolButton:pressed {{ background:{PALETTE.bg2}; }}"
            "QTabBar QToolButton:disabled{ color:#555; }"
        )
        tab_lay.addWidget(self._tab_bar)

        tab_lay.addSpacing(gap)

        self.new_tab_btn = QPushButton("+")
        self.new_tab_btn.setFixedSize(btn_w, btn_h)
        self.new_tab_btn.setToolTip(self.tr("New tab (Ctrl+T)"))
        self.new_tab_btn.setStyleSheet(
            "QPushButton {"
            "  padding:0; border:none; background:transparent;"
            f"  font-size:17px; font-weight:300; color:{PALETTE.text_muted}; min-width:0; }}"
            f"QPushButton:hover  {{ background:{PALETTE.bg3}; border-radius:4px;"
            f" color:{PALETTE.text_primary}; }}"
            f"QPushButton:pressed{{ background:{PALETTE.bg2}; color:{PALETTE.text_secondary}; }}"
        )
        self.new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_tab_btn.clicked.connect(lambda: self._new_tab())
        tab_lay.addWidget(self.new_tab_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        tab_lay.addStretch(1)

        reserved = gap + gap + btn_w + gap

        class _RowResizeFilter(QObject):
            def __init__(self_, bar, reserved_width, parent=None):
                super().__init__(parent)
                self_._bar = bar
                self_._reserved = reserved_width

            def eventFilter(self_, obj, event):
                if event.type() == QEvent.Type.Resize:
                    self_._bar.setMaximumWidth(
                        max(60, obj.width() - self_._reserved)
                    )
                return False

        self._row_resize_filter = _RowResizeFilter(self._tab_bar, reserved, tab_row)
        tab_row.installEventFilter(self._row_resize_filter)
        self._tab_bar.setMaximumWidth(max(60, tab_row.width() - reserved))

        root.addWidget(tab_row)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, stretch=1)

        self.loading_bar = QFrame()
        self.loading_bar.setFixedHeight(2)
        self.loading_bar.setStyleSheet(f"background:{PALETTE.accent};")
        self.loading_bar.setVisible(False)
        root.addWidget(self.loading_bar)
