from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
)

from ...styles.icons import ICON_ASPECT_MATCH, ICON_CAST, ICON_CROP, make_icon
from ...styles.theme import PALETTE, qss_rgba
from .tab_bar import BrowserTabBar


__all__ = ("BrowserUiMixin",)


class BrowserUrlBar(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("UrlBarFrame")
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setProperty("focused", False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        layout.setSpacing(6)

        self.line_edit = QLineEdit(self)
        self.line_edit.setObjectName("UrlBar")
        self.line_edit.setFrame(False)
        self.line_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.line_edit.installEventFilter(self)
        layout.addWidget(self.line_edit)

        self.zoom_indicator = QLabel(self)
        self.zoom_indicator.setObjectName("ZoomIndicator")
        self.zoom_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_indicator.setFixedWidth(52)
        self.zoom_indicator.hide()
        layout.addWidget(self.zoom_indicator)

        self.apply_theme()

    def eventFilter(self, watched, event):
        if watched is self.line_edit and event.type() in {
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        }:
            self.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        return super().eventFilter(watched, event)

    def set_zoom_factor(self, factor: float) -> None:
        factor = float(factor)
        percent = factor * 100
        nearest_integer = round(percent)
        if abs(percent - nearest_integer) <= 1e-6:
            text = f"{nearest_integer}%"
        else:
            text = f"{percent:.1f}%"
        self.zoom_indicator.setText(text)
        self.zoom_indicator.setVisible(abs(factor - 1.0) > 1e-6)

    def apply_theme(self) -> None:
        self.setStyleSheet(
            "QFrame#UrlBarFrame {"
            f"  border:1px solid {PALETTE.border}; border-radius:6px;"
            f"  background:{PALETTE.bg0}; }}"
            "QFrame#UrlBarFrame[focused=\"true\"] {"
            f"  border-color:{PALETTE.accent}; }}"
            "QLineEdit#UrlBar {"
            "  border:none; padding:0; font-size:12px; background:transparent;"
            f"  color:{PALETTE.text_secondary};"
            f"  selection-background-color:{PALETTE.accent}; }}"
            "QLabel#ZoomIndicator {"
            f"  border:none; border-left:1px solid {PALETTE.border_muted};"
            "  padding:0 4px 0 8px; font-size:11px; font-weight:600;"
            f"  background:transparent; color:{PALETTE.text_secondary}; }}"
        )


class BrowserUiMixin:
    @staticmethod
    def _std_icon(sp) -> QIcon:
        return QApplication.style().standardIcon(sp)

    @staticmethod
    def _browser_nav_button_style() -> str:
        return (
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

    @staticmethod
    def _browser_action_button_style(color: str) -> str:
        active_bg = qss_rgba(color, 0.18)
        hover_bg = qss_rgba(color, 0.26)
        pressed_bg = qss_rgba(color, 0.12)
        return (
            "QPushButton {"
            f"  padding:0; border:1px solid {color}; border-radius:6px;"
            f"  background:{active_bg}; min-width:0; }}"
            f"QPushButton:hover:enabled  {{ background:{hover_bg};"
            f" border-color:{color}; }}"
            f"QPushButton:pressed:enabled{{ background:{pressed_bg}; }}"
        )

    @staticmethod
    def _browser_tab_bar_style(row_h: int, btn_h: int, btn_w: int) -> str:
        return (
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
            f"QTabBar QToolButton:disabled{{ color:{PALETTE.text_dim}; }}"
        )

    @staticmethod
    def _browser_new_tab_style() -> str:
        return (
            "QPushButton {"
            "  padding:0; border:none; background:transparent;"
            f"  font-size:17px; font-weight:300; color:{PALETTE.text_muted}; min-width:0; }}"
            f"QPushButton:hover  {{ background:{PALETTE.bg3}; border-radius:4px;"
            f" color:{PALETTE.text_primary}; }}"
            f"QPushButton:pressed{{ background:{PALETTE.bg2}; color:{PALETTE.text_secondary}; }}"
        )

    def _apply_browser_theme_styles(self) -> None:
        btn_style = self._browser_nav_button_style()
        self._cast_btn_style_off = btn_style
        self._crop_btn_style_off = btn_style
        self._cursor_btn_style_off = btn_style
        self._aspect_btn_style_off = btn_style
        self._cast_btn_style_on = self._browser_action_button_style(PALETTE.accent)
        self._crop_btn_style_on = self._browser_action_button_style(PALETTE.warning)
        self._cursor_btn_style_on = self._browser_action_button_style(PALETTE.projection)
        self._aspect_btn_style_on = self._browser_action_button_style(PALETTE.accent_alt)

        for button_name in ("back_btn", "fwd_btn", "reload_btn"):
            if hasattr(self, button_name):
                getattr(self, button_name).setStyleSheet(btn_style)
        if hasattr(self, "_url_bar"):
            self._url_bar.apply_theme()
        if hasattr(self, "_browser_tab_row"):
            self._browser_tab_row.setStyleSheet(
                f"QFrame {{ background:{PALETTE.surface}; border:none; }}"
            )
        if hasattr(self, "_tab_bar"):
            self._tab_bar.setStyleSheet(
                self._browser_tab_bar_style(self._tab_row_h, self._tab_btn_h, self._tab_btn_w)
            )
        if hasattr(self, "new_tab_btn"):
            self.new_tab_btn.setStyleSheet(self._browser_new_tab_style())
        if hasattr(self, "loading_bar"):
            self.loading_bar.setStyleSheet(f"background:{PALETTE.accent};")

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

        btn_style = self._browser_nav_button_style()

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

        self._url_bar = BrowserUrlBar()
        self.url_edit = self._url_bar.line_edit
        self.url_edit.setPlaceholderText(self.tr("Paste or type a URL…"))
        self.url_edit.setCursor(Qt.CursorShape.IBeamCursor)
        self.url_edit.returnPressed.connect(self._navigate_from_bar)
        nav.addWidget(self._url_bar)
        nav.addSpacing(4)

        self.cast_btn = QPushButton()
        self.cast_btn.setFixedSize(34, 34)
        self.cast_btn.setCheckable(True)
        self.cast_btn.setToolTip(self.tr("Project this tab live"))
        self.cast_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cast_btn_style_off = btn_style
        self._cast_btn_style_on = self._browser_action_button_style(PALETTE.accent)
        self.cast_btn.setIcon(make_icon(ICON_CAST, 16, PALETTE.text_muted))
        self.cast_btn.setStyleSheet(self._cast_btn_style_off)
        self.cast_btn.clicked.connect(self._on_cast_clicked)
        nav.addWidget(self.cast_btn)

        self._crop_btn_style_off = btn_style
        self._crop_btn_style_on = self._browser_action_button_style(PALETTE.warning)
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
        self._cursor_btn_style_on = self._browser_action_button_style(PALETTE.projection)
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
        self._aspect_btn_style_on = self._browser_action_button_style(PALETTE.accent_alt)
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
        self._tab_row_h = row_h
        self._tab_btn_h = btn_h
        self._tab_btn_w = btn_w

        tab_row = QFrame()
        self._browser_tab_row = tab_row
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
        self._tab_bar.setStyleSheet(self._browser_tab_bar_style(row_h, btn_h, btn_w))
        tab_lay.addWidget(self._tab_bar)

        tab_lay.addSpacing(gap)

        self.new_tab_btn = QPushButton("+")
        self.new_tab_btn.setFixedSize(btn_w, btn_h)
        self.new_tab_btn.setToolTip(self.tr("New tab (Ctrl+T)"))
        self.new_tab_btn.setStyleSheet(self._browser_new_tab_style())
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
