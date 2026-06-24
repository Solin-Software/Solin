"""Monitor manager popup for projection outputs."""

from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, QEvent, QSize, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from solin.core.media.formats import media_type_from_path
from solin.styles.icons import ICON_CLOSE, ICON_IMAGE, ICON_MONITOR, ICON_TV, ICON_VIDEO, make_icon
from solin.styles.theme import PALETTE, qss_rgba

# ── Monitor Manager Popup ─────────────────────────────────────────────────────

class MonitorManagerPopup(QWidget):
    """
    Floating popup to manage projection windows and the idle screen.

    Sections
    --------
    1. Header  — title + close hint
    2. Monitor rows — one per secondary screen + floating preview row
    3. Footer actions — "Project all" / "Remove all" (full-width, no truncation)
    4. Idle Screen section — pick a custom image/video for the idle background
                             (session-only, never persisted)
    """

    # Signals
    projection_toggle_requested = Signal(int, bool)   # (screen_index, active)
    projection_all_requested    = Signal(bool)         # True=show all / False=remove all
    floating_toggle_requested   = Signal(bool)         # True=show / False=hide
    idle_media_changed          = Signal(str)          # path or "" to clear

    _POPUP_W = 340

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFixedWidth(self._POPUP_W)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._screens_info: list[dict] = []
        self._idle_media_path: str = ""
        self._floating_active = False
        self._dividers: list[QFrame] = []

        # Opacity animation
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(180)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        self._build_ui()

    # ─────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("MonitorPopupCard")
        self._card.setStyleSheet(self._card_style())
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet("background: transparent;")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(16, 14, 16, 10)
        h_lay.setSpacing(8)

        self._header_icon_lbl = QLabel()
        self._header_icon_lbl.setPixmap(
            make_icon(ICON_MONITOR, 15, PALETTE.text_muted).pixmap(15, 15)
        )
        self._header_icon_lbl.setStyleSheet("background: transparent;")

        self._header_title_lbl = QLabel(self.tr("Monitors"))
        self._header_title_lbl.setStyleSheet(self._header_title_style())
        h_lay.addWidget(self._header_icon_lbl)
        h_lay.addWidget(self._header_title_lbl)
        h_lay.addStretch()

        card_lay.addWidget(header)

        # ── Divider ───────────────────────────────────────────────────────
        card_lay.addWidget(self._make_divider())

        # ── Monitor list area ─────────────────────────────────────────────
        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_lay = QVBoxLayout(self._list_widget)
        self._list_lay.setContentsMargins(10, 8, 10, 8)
        self._list_lay.setSpacing(4)
        card_lay.addWidget(self._list_widget)

        # ── Footer: Project all / Remove all ─────────────────────────────
        card_lay.addWidget(self._make_divider())

        footer = QWidget()
        footer.setStyleSheet("background: transparent;")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 8, 10, 8)
        f_lay.setSpacing(8)

        self._all_on_btn = self._make_action_btn(
            self.tr("Project all"),
            PALETTE.accent,
            qss_rgba(PALETTE.accent, 0.10),
            qss_rgba(PALETTE.accent, 0.20),
        )
        self._all_off_btn = self._make_action_btn(
            self.tr("Remove all"),
            PALETTE.danger,
            qss_rgba(PALETTE.danger, 0.10),
            qss_rgba(PALETTE.danger, 0.20),
        )

        self._all_on_btn.clicked.connect(lambda: self.projection_all_requested.emit(True))
        self._all_off_btn.clicked.connect(lambda: self.projection_all_requested.emit(False))

        f_lay.addWidget(self._all_on_btn)
        f_lay.addWidget(self._all_off_btn)
        card_lay.addWidget(footer)

        # ── Idle Screen section ───────────────────────────────────────────
        card_lay.addWidget(self._make_divider())
        card_lay.addWidget(self._build_idle_section())

        root.addWidget(self._card)

    @staticmethod
    def _card_style() -> str:
        return (
            "QFrame#MonitorPopupCard {"
            f"  background: {PALETTE.surface};"
            f"  border: 1px solid {PALETTE.border};"
            "  border-radius: 12px;"
            "}"
        )

    @staticmethod
    def _header_title_style() -> str:
        return (
            f"color: {PALETTE.text_primary}; font-size: 13px; font-weight: 600;"
            " background: transparent;"
        )

    @staticmethod
    def _divider_style() -> str:
        return (
            f"background: {PALETTE.border_muted}; border: none;"
            " max-height: 1px; min-height: 1px;"
        )

    @staticmethod
    def _action_btn_style(color: str, hover_bg: str, press_bg: str) -> str:
        return (
            "QPushButton {"
            f"  color: {color};"
            "  background: transparent;"
            f"  border: 1px solid {color};"
            "  border-radius: 7px;"
            "  font-size: 12px;"
            "  font-weight: 500;"
            "  padding: 0 8px;"
            "}"
            f"QPushButton:hover {{ background: {hover_bg}; }}"
            f"QPushButton:pressed {{ background: {press_bg}; }}"
            "QPushButton:disabled {"
            f"  color: {PALETTE.text_dim}; border-color: {PALETTE.border};"
            "}"
        )

    def _make_divider(self) -> QFrame:
        d = QFrame()
        d.setFrameShape(QFrame.Shape.HLine)
        d.setStyleSheet(self._divider_style())
        d.setFixedHeight(1)
        self._dividers.append(d)
        return d

    def _make_action_btn(self, text: str, color: str, hover_bg: str, press_bg: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setFixedHeight(32)
        btn.setStyleSheet(self._action_btn_style(color, hover_bg, press_bg))
        return btn

    def _build_idle_section(self) -> QWidget:
        """Section to select a custom idle background (image or video)."""
        section = QWidget()
        section.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(section)
        lay.setContentsMargins(10, 10, 10, 12)
        lay.setSpacing(8)

        # Section title row
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.setContentsMargins(0, 0, 0, 0)

        self._idle_tv_icon = QLabel()
        self._idle_tv_icon.setPixmap(
            make_icon(ICON_TV, 13, PALETTE.text_muted).pixmap(13, 13)
        )
        self._idle_tv_icon.setStyleSheet("background: transparent;")

        self._idle_section_title_lbl = QLabel(self.tr("Idle Screen"))
        self._idle_section_title_lbl.setStyleSheet(
            f"color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
            " letter-spacing: 0.5px; background: transparent;"
        )
        title_row.addWidget(self._idle_tv_icon)
        title_row.addWidget(self._idle_section_title_lbl)
        title_row.addStretch()
        lay.addLayout(title_row)

        # Media picker row
        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        picker_row.setContentsMargins(0, 0, 0, 0)

        # Icon label (changes with file type)
        self._idle_type_icon = QLabel()
        self._idle_type_icon.setFixedSize(18, 18)
        self._idle_type_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_type_icon.setStyleSheet("background: transparent;")
        self._idle_type_icon.setPixmap(make_icon(ICON_IMAGE, 14, PALETTE.text_dim).pixmap(14, 14))

        # File name / placeholder label
        self._idle_name_lbl = QLabel(self.tr("No media selected"))
        self._idle_name_lbl.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px; background: transparent;"
        )
        self._idle_name_lbl.setMaximumWidth(180)
        # Truncate long names with ellipsis on the left (shows filename end)
        self._idle_name_lbl.setTextFormat(Qt.TextFormat.PlainText)

        # Pick button
        self._idle_pick_btn = QPushButton(self.tr("Choose…"))
        self._idle_pick_btn.setFixedHeight(28)
        self._idle_pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._idle_pick_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: {PALETTE.text_muted};"
            f"  background: {PALETTE.bg2};"
            f"  border: 1px solid {PALETTE.border};"
            f"  border-radius: 6px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"  padding: 0 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {PALETTE.border}; border-color: {PALETTE.text_dim}; color: {PALETTE.text_secondary};"
            f"}}"
            f"QPushButton:pressed {{ background: {PALETTE.surface}; }}"
        )
        self._idle_pick_btn.clicked.connect(self._on_pick_idle_media)

        # Clear button (shown only when media is set)
        self._idle_clear_btn = QPushButton()
        self._idle_clear_btn.setFixedSize(28, 28)
        self._idle_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._idle_clear_btn.setIcon(make_icon(ICON_CLOSE, 11, PALETTE.danger))
        self._idle_clear_btn.setIconSize(QSize(11, 11))
        self._idle_clear_btn.setToolTip(self.tr("Remove idle media"))
        self._idle_clear_btn.setStyleSheet(
            "QPushButton {"
            "  background: transparent;"
            "  border: 1px solid transparent;"
            "  border-radius: 6px;"
            "}"
            f"QPushButton:hover {{"
            f"  background: {qss_rgba(PALETTE.danger, 0.10)};"
            f"  border-color: {qss_rgba(PALETTE.danger, 0.40)};"
            f"}}"
            f"QPushButton:pressed {{ background: {qss_rgba(PALETTE.danger, 0.20)}; }}"
        )
        self._idle_clear_btn.setVisible(False)
        self._idle_clear_btn.clicked.connect(self._on_clear_idle_media)

        picker_row.addWidget(self._idle_type_icon)
        picker_row.addWidget(self._idle_name_lbl, stretch=1)
        picker_row.addWidget(self._idle_pick_btn)
        picker_row.addWidget(self._idle_clear_btn)
        lay.addLayout(picker_row)

        # Hint label
        self._idle_hint_lbl = QLabel(self.tr("Session only · not saved on exit"))
        self._idle_hint_lbl.setStyleSheet(
            f"color: {PALETTE.border}; font-size: 10px; background: transparent;"
        )
        lay.addWidget(self._idle_hint_lbl)

        return section

    def apply_theme(self) -> None:
        self._card.setStyleSheet(self._card_style())
        self._header_icon_lbl.setPixmap(
            make_icon(ICON_MONITOR, 15, PALETTE.text_muted).pixmap(15, 15)
        )
        self._header_title_lbl.setStyleSheet(self._header_title_style())
        for divider in self._dividers:
            divider.setStyleSheet(self._divider_style())
        self._all_on_btn.setStyleSheet(
            self._action_btn_style(
                PALETTE.accent,
                qss_rgba(PALETTE.accent, 0.10),
                qss_rgba(PALETTE.accent, 0.20),
            )
        )
        self._all_off_btn.setStyleSheet(
            self._action_btn_style(
                PALETTE.danger,
                qss_rgba(PALETTE.danger, 0.10),
                qss_rgba(PALETTE.danger, 0.20),
            )
        )
        self._idle_tv_icon.setPixmap(
            make_icon(ICON_TV, 13, PALETTE.text_muted).pixmap(13, 13)
        )
        self._idle_section_title_lbl.setStyleSheet(
            f"color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
            " letter-spacing: 0.5px; background: transparent;"
        )
        self._idle_pick_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: {PALETTE.text_muted};"
            f"  background: {PALETTE.bg2};"
            f"  border: 1px solid {PALETTE.border};"
            f"  border-radius: 6px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"  padding: 0 10px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {PALETTE.border}; border-color: {PALETTE.text_dim};"
            f"  color: {PALETTE.text_secondary};"
            f"}}"
            f"QPushButton:pressed {{ background: {PALETTE.surface}; }}"
        )
        self._idle_clear_btn.setIcon(make_icon(ICON_CLOSE, 11, PALETTE.danger))
        self._idle_clear_btn.setStyleSheet(
            "QPushButton {"
            "  background: transparent;"
            "  border: 1px solid transparent;"
            "  border-radius: 6px;"
            "}"
            f"QPushButton:hover {{"
            f"  background: {qss_rgba(PALETTE.danger, 0.10)};"
            f"  border-color: {qss_rgba(PALETTE.danger, 0.40)};"
            f"}}"
            f"QPushButton:pressed {{ background: {qss_rgba(PALETTE.danger, 0.20)}; }}"
        )
        self._idle_hint_lbl.setStyleSheet(
            f"color: {PALETTE.border}; font-size: 10px; background: transparent;"
        )
        self.populate(
            self._screens_info,
            floating_active=self._floating_active,
            idle_media_path=self._idle_media_path,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Populate
    # ─────────────────────────────────────────────────────────────────────

    def populate(self, screens_info: list[dict], floating_active: bool = False,
                 idle_media_path: str = ""):
        """Rebuild monitor rows and sync the idle media state."""
        self._screens_info = screens_info
        self._idle_media_path = idle_media_path
        self._floating_active = bool(floating_active)

        # Clear old rows
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Floating preview row (always present)
        self._list_lay.addWidget(self._make_floating_row(floating_active))

        if not screens_info:
            empty = QLabel(self.tr("No secondary monitors detected"))
            empty.setStyleSheet(
                f"color: {PALETTE.text_dim}; font-size: 12px; background: transparent;"
                " padding: 6px 6px 8px 6px;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._list_lay.addWidget(empty)
            self._all_on_btn.setEnabled(False)
            self._all_off_btn.setEnabled(False)
        else:
            self._all_on_btn.setEnabled(True)
            self._all_off_btn.setEnabled(True)
            for info in screens_info:
                self._list_lay.addWidget(self._make_row(info))

        # Sync idle media display
        self._sync_idle_ui(idle_media_path)
        self.adjustSize()

    def _sync_idle_ui(self, path: str):
        """Update idle section to reflect current path (empty = no media)."""
        self._idle_media_path = path
        if path:
            import os
            name = os.path.basename(path)
            # Truncate long names
            max_len = 24
            display = ("…" + name[-(max_len - 1):]) if len(name) > max_len else name
            self._idle_name_lbl.setText(display)
            self._idle_name_lbl.setStyleSheet(
                f"color: {PALETTE.text_secondary}; font-size: 11px; background: transparent;"
            )
            # Pick icon based on extension
            mtype = media_type_from_path(path)
            icon_svg = ICON_VIDEO if mtype == "video" else ICON_IMAGE
            icon_color = PALETTE.success if mtype == "video" else PALETTE.accent_hover
            self._idle_type_icon.setPixmap(make_icon(icon_svg, 14, icon_color).pixmap(14, 14))
            self._idle_clear_btn.setVisible(True)
        else:
            self._idle_name_lbl.setText(self.tr("No media selected"))
            self._idle_name_lbl.setStyleSheet(
                f"color: {PALETTE.text_dim}; font-size: 11px; background: transparent;"
            )
            self._idle_type_icon.setPixmap(make_icon(ICON_IMAGE, 14, PALETTE.text_dim).pixmap(14, 14))
            self._idle_clear_btn.setVisible(False)

    # ─────────────────────────────────────────────────────────────────────
    # Row builders
    # ─────────────────────────────────────────────────────────────────────

    def _make_floating_row(self, active: bool) -> QWidget:
        return self._make_monitor_row(
            icon_svg=ICON_MONITOR,
            name=self.tr("Windowed Preview"),
            subtitle=self.tr("Primary screen · shareable window"),
            active=active,
            on_toggle=lambda a: self.floating_toggle_requested.emit(not a),
        )

    def _make_row(self, info: dict) -> QWidget:
        idx     = info["index"]
        screen  = info["screen"]
        active  = info["active"]
        reserved = info.get("timer_reserved", False)
        geo     = screen.geometry()
        name    = screen.name() or f"Monitor {idx + 1}"
        res_str = f"{geo.width()} × {geo.height()}"
        if reserved:
            res_str = self.tr("Reserved by the timer · {res}").replace("{res}", res_str)
        return self._make_monitor_row(
            icon_svg=ICON_MONITOR,
            name=name,
            subtitle=res_str,
            active=active,
            on_toggle=lambda a, i=idx: self.projection_toggle_requested.emit(i, not a),
            reserved=reserved,
        )

    def _make_monitor_row(self, icon_svg, name: str, subtitle: str,
                          active: bool, on_toggle, reserved: bool = False) -> QWidget:
        row = QFrame()
        row.setObjectName("MonitorRow")
        row.setStyleSheet(
            f"QFrame#MonitorRow {{"
            f"  background: {PALETTE.bg0};"
            f"  border: 1px solid {PALETTE.border_muted};"
            f"  border-radius: 8px;"
            f"}}"
            f"QFrame#MonitorRow:hover {{"
            f"  border-color: {PALETTE.border};"
            f"  background: {PALETTE.surface};"
            f"}}"
        )
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(12, 10, 10, 10)
        row_lay.setSpacing(10)

        # Icon container with status dot
        icon_container = QWidget()
        icon_container.setFixedSize(32, 32)
        icon_container.setStyleSheet("background: transparent;")
        icon_lbl = QLabel(icon_container)
        icon_lbl.setGeometry(0, 0, 32, 32)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_color = PALETTE.success if active else PALETTE.text_dim
        icon_lbl.setPixmap(make_icon(icon_svg, 18, icon_color).pixmap(18, 18))

        dot = QLabel(icon_container)
        dot.setFixedSize(8, 8)
        dot.move(22, 22)
        dot.setStyleSheet(
            f"background: {PALETTE.success if active else PALETTE.text_dim};"
            f"border-radius: 4px;"
            f"border: 1.5px solid {PALETTE.surface};"
        )

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        sub_lbl = QLabel(subtitle)
        sub_lbl.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 10px; background: transparent;"
        )
        text_col.addWidget(name_lbl)
        text_col.addWidget(sub_lbl)

        # Toggle button — fixed min-width so short/long labels are handled
        if active:
            btn_text, btn_color, btn_hover, btn_press = (
                self.tr("Hide"), PALETTE.danger,
                qss_rgba(PALETTE.danger, 0.10), qss_rgba(PALETTE.danger, 0.20)
            )
        elif reserved:
            # Inactive *and* reserved by the timer — offer to take it over
            # (the controller will ask for confirmation before displacing it).
            btn_text, btn_color, btn_hover, btn_press = (
                self.tr("Use here"), PALETTE.warning,
                qss_rgba(PALETTE.warning, 0.10), qss_rgba(PALETTE.warning, 0.20)
            )
        else:
            btn_text, btn_color, btn_hover, btn_press = (
                self.tr("Show"), PALETTE.accent,
                qss_rgba(PALETTE.accent, 0.10), qss_rgba(PALETTE.accent, 0.20)
            )

        toggle_btn = QPushButton(btn_text)
        toggle_btn.setFixedHeight(28)
        toggle_btn.setMinimumWidth(64)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: {btn_color};"
            f"  background: transparent;"
            f"  border: 1px solid {btn_color};"
            f"  border-radius: 6px;"
            f"  font-size: 11px;"
            f"  font-weight: 500;"
            f"  padding: 0 12px;"
            f"}}"
            f"QPushButton:hover {{ background: {btn_hover}; }}"
            f"QPushButton:pressed {{ background: {btn_press}; }}"
        )
        toggle_btn.clicked.connect(lambda _checked, a=active: on_toggle(a))

        row_lay.addWidget(icon_container)
        row_lay.addLayout(text_col, stretch=1)
        row_lay.addWidget(toggle_btn)
        return row

    # ─────────────────────────────────────────────────────────────────────
    # Idle media handlers
    # ─────────────────────────────────────────────────────────────────────

    def _on_pick_idle_media(self):
        """Open file dialog and emit idle_media_changed if user picks a file.

        Qt.WindowType.Popup closes automatically when it loses focus, which
        means the dialog would never appear if opened synchronously from inside
        the popup.  We hide first, then schedule the dialog via a zero-timer so
        the event loop finishes closing the popup before the dialog opens.
        """
        start_dir = os.path.dirname(self._idle_media_path) if self._idle_media_path else ""
        self.hide()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._open_file_dialog(start_dir))

    def _open_file_dialog(self, start_dir: str):
        """Slot deferred from _on_pick_idle_media — runs after popup is gone."""
        supported = (
            "Media files (*.jpg *.jpeg *.png *.bmp *.gif *.webp *.tiff *.tif "
            "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv);;"
            "Images (*.jpg *.jpeg *.png *.bmp *.gif *.webp *.tiff *.tif);;"
            "Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv);;"
            "All files (*)"
        )
        # Use the MainWindow as parent so the dialog renders on the primary screen.
        parent_win = self.parent() if self.parent() else None
        path, _ = QFileDialog.getOpenFileName(
            parent_win, self.tr("Choose idle screen media"), start_dir, supported
        )
        if path:
            self._sync_idle_ui(path)
            self.idle_media_changed.emit(path)

    def _on_clear_idle_media(self):
        """Clear idle media selection."""
        self._sync_idle_ui("")
        self.idle_media_changed.emit("")
        self.adjustSize()

    # ─────────────────────────────────────────────────────────────────────
    # Show / hide
    # ─────────────────────────────────────────────────────────────────────

    def show_above(self, anchor: QWidget):
        """Position the popup above (or below if no room) anchor and fade in."""
        already_vis = self.isVisible()
        self.adjustSize()
        global_pos = anchor.mapToGlobal(anchor.rect().topLeft())
        x = global_pos.x()
        y = global_pos.y() - self.height() - 8
        screen_geo = (
            anchor.screen().availableGeometry()
            if hasattr(anchor, "screen")
            else QGuiApplication.primaryScreen().availableGeometry()
        )
        if x + self.width() > screen_geo.right():
            x = screen_geo.right() - self.width() - 4
        if y < screen_geo.top():
            y = global_pos.y() + anchor.height() + 8
        self.move(x, y)
        if already_vis:
            # Already showing — just reposition, no fade
            return
        self._opacity_effect.setOpacity(0.0)
        self.show()
        self.raise_()
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def hide_animated(self):
        """Fade out then hide."""
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.0)
        try:
            self._fade_anim.finished.disconnect()
        except RuntimeError:
            pass
        self._fade_anim.finished.connect(self.hide)
        self._fade_anim.start()

    def hideEvent(self, event):
        try:
            self._fade_anim.finished.disconnect(self.hide)
        except RuntimeError:
            pass
        super().hideEvent(event)

    # ─────────────────────────────────────────────────────────────────────
    # i18n – retranslation support
    # ─────────────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        """Re-apply all translatable texts when the UI language changes."""
        self._header_title_lbl.setText(self.tr("Monitors"))
        self._all_on_btn.setText(self.tr("Project all"))
        self._all_off_btn.setText(self.tr("Remove all"))
        self._idle_section_title_lbl.setText(self.tr("Idle Screen"))
        self._idle_pick_btn.setText(self.tr("Choose…"))
        self._idle_hint_lbl.setText(self.tr("Session only · not saved on exit"))
        self._idle_clear_btn.setToolTip(self.tr("Remove idle media"))


# ── Widget global de projeção (rodapé direito) ────────────────────────────────

