from __future__ import annotations

import sys

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...styles.icons import ICON_CROSSHAIR, ICON_EDIT, ICON_SHARE_SCREEN, make_icon
from .shared import (
    SETTINGS_BG,
    SETTINGS_BORDER,
    SETTINGS_DIM,
    SETTINGS_SUCCESS,
    SETTINGS_MUTED,
    SETTINGS_DANGER,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SettingsToggleSwitch,
    settings_compact_secondary_button_stylesheet,
)
from .auto_key_dialog import AutoKeyEditorDialog


class AutoShareSectionMixin:
    """Builds and manages automatic Zoom screen sharing settings."""

    def _autoshare_hotkey(self) -> str:
        return self._auto_share_settings.ensure_hotkey()

    def _autoshare_accessibility_trusted(self) -> bool:
        if sys.platform != "darwin":
            return True
        return bool(self._auto_share_accessibility_trusted())

    def _build_auto_share_card(self):
        card, lay = self._card()

        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 10, 14, 10)
        header_lay.setSpacing(12)
        share_icon = QLabel()
        self._autoshare_icon_lbl = share_icon
        share_icon.setPixmap(
            make_icon(ICON_SHARE_SCREEN, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        share_icon.setFixedSize(20, 20)
        share_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        share_icon.setStyleSheet("background: transparent; border: none;")
        header_lay.addWidget(share_icon)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._autoshare_label = QLabel(self.tr("Auto Screen Share"))
        self._autoshare_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        col.addWidget(self._autoshare_label)
        self._autoshare_desc = QLabel(
            self.tr("Automatically shares screen via hotkeys when projecting media.")
        )
        self._autoshare_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        col.addWidget(self._autoshare_desc)
        header_lay.addLayout(col, stretch=1)
        saved_enabled = self._auto_share_settings.is_enabled()
        self._autoshare_toggle = SettingsToggleSwitch(checked=saved_enabled)
        self._autoshare_toggle.toggled.connect(self._on_autoshare_toggled)
        header_lay.addWidget(self._autoshare_toggle)
        lay.addWidget(header)

        self._autoshare_container = QFrame()
        self._autoshare_container.setStyleSheet(
            f"background: {SETTINGS_BG}; border: none;"
            f" border-top: 1px solid {SETTINGS_BORDER};"
        )
        as_lay = QVBoxLayout(self._autoshare_container)
        as_lay.setContentsMargins(14, 12, 14, 12)
        as_lay.setSpacing(10)

        hotkey_row = QFrame()
        hotkey_row.setStyleSheet("background: transparent; border: none;")
        hotkey_row_lay = QHBoxLayout(hotkey_row)
        hotkey_row_lay.setContentsMargins(0, 0, 0, 0)
        hotkey_row_lay.setSpacing(10)

        hotkey_text_col = QVBoxLayout()
        hotkey_text_col.setSpacing(1)
        self._autoshare_hotkey_lbl = QLabel(self.tr("Share hotkey"))
        self._autoshare_hotkey_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        hotkey_text_col.addWidget(self._autoshare_hotkey_lbl)
        self._autoshare_hotkey_hint = QLabel(
            self.tr("Uses Zoom's single start/stop screen-share shortcut.")
        )
        self._autoshare_hotkey_hint.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._autoshare_hotkey_hint.setWordWrap(True)
        hotkey_text_col.addWidget(self._autoshare_hotkey_hint)
        hotkey_row_lay.addLayout(hotkey_text_col, stretch=1)

        saved_hotkey = self._autoshare_hotkey()
        self._autoshare_hotkey_value = QLabel(saved_hotkey or self.tr("Not configured"))
        self._autoshare_hotkey_value.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {SETTINGS_SUCCESS if saved_hotkey else SETTINGS_DIM};"
            " background: transparent; border: none;"
        )
        hotkey_row_lay.addWidget(self._autoshare_hotkey_value)

        self._autoshare_hotkey_btn = self._auto_key_icon_button(ICON_EDIT, self.tr("Edit"))
        self._autoshare_hotkey_btn.clicked.connect(self._on_autoshare_hotkey_edit)
        hotkey_row_lay.addWidget(self._autoshare_hotkey_btn)
        as_lay.addWidget(hotkey_row)

        if sys.platform == "darwin":
            self._autoshare_access_row = QFrame()
            self._autoshare_access_row.setStyleSheet(
                f"background: {SETTINGS_SURFACE}; border: 1px solid {SETTINGS_BORDER}; border-radius: 8px;"
            )
            access_lay = QHBoxLayout(self._autoshare_access_row)
            access_lay.setContentsMargins(10, 8, 8, 8)
            access_lay.setSpacing(10)

            self._autoshare_access_dot = QLabel()
            self._autoshare_access_dot.setFixedSize(8, 8)
            access_lay.addWidget(self._autoshare_access_dot)

            access_text_col = QVBoxLayout()
            access_text_col.setSpacing(1)
            self._autoshare_access_title = QLabel(self.tr("Accessibility permission"))
            self._autoshare_access_title.setStyleSheet(
                f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            )
            access_text_col.addWidget(self._autoshare_access_title)
            self._autoshare_access_desc = QLabel()
            self._autoshare_access_desc.setWordWrap(True)
            self._autoshare_access_desc.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
            )
            access_text_col.addWidget(self._autoshare_access_desc)
            access_lay.addLayout(access_text_col, stretch=1)

            self._autoshare_access_btn = QPushButton(self.tr("Open Settings"))
            self._autoshare_access_btn.setFixedHeight(28)
            self._autoshare_access_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._autoshare_access_btn.setStyleSheet(
                settings_compact_secondary_button_stylesheet()
            )
            self._autoshare_access_btn.clicked.connect(self._open_macos_accessibility_settings)
            access_lay.addWidget(self._autoshare_access_btn)
            as_lay.addWidget(self._autoshare_access_row)
            self._refresh_autoshare_accessibility_status()

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {SETTINGS_BORDER}; border: none;")
        self._autoshare_separator = sep
        as_lay.addWidget(sep)

        qc_row = QFrame()
        qc_row.setStyleSheet("background: transparent; border: none;")
        qc_row_lay = QHBoxLayout(qc_row)
        qc_row_lay.setContentsMargins(0, 0, 0, 0)
        qc_row_lay.setSpacing(12)

        qc_icon = QLabel()
        self._autoshare_crosshair_icon_lbl = qc_icon
        qc_icon.setPixmap(
            make_icon(ICON_CROSSHAIR, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        qc_icon.setFixedSize(20, 20)
        qc_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qc_icon.setStyleSheet("background: transparent; border: none;")
        qc_row_lay.addWidget(qc_icon)

        qc_col = QVBoxLayout()
        qc_col.setSpacing(1)
        self._autoshare_pos_title = QLabel(self.tr("Click Position"))
        self._autoshare_pos_title.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        qc_col.addWidget(self._autoshare_pos_title)
        self._autoshare_pos_desc = QLabel(
            self.tr("Position to click after the share dialog opens to select the target.")
        )
        self._autoshare_pos_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._autoshare_pos_desc.setWordWrap(True)
        qc_col.addWidget(self._autoshare_pos_desc)
        qc_row_lay.addLayout(qc_col, stretch=1)
        as_lay.addWidget(qc_row)

        qc_cfg_row = QFrame()
        qc_cfg_row.setStyleSheet("background: transparent; border: none;")
        qc_cfg_lay = QHBoxLayout(qc_cfg_row)
        qc_cfg_lay.setContentsMargins(32, 2, 0, 0)
        qc_cfg_lay.setSpacing(10)

        saved_x, saved_y = self._auto_share_settings.click_position()
        has_pos = saved_x >= 0 and saved_y >= 0

        self._autoshare_pos_status = QLabel()
        self._autoshare_pos_status.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_SUCCESS if has_pos else SETTINGS_DIM};"
            " background: transparent; border: none;"
        )
        qc_cfg_lay.addWidget(self._autoshare_pos_status, stretch=1)
        self._refresh_autoshare_position_label()

        self._autoshare_config_btn = QPushButton(self.tr("Configure"))
        self._autoshare_config_btn.setFixedHeight(28)
        self._autoshare_config_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autoshare_config_btn.setIcon(
            make_icon(ICON_CROSSHAIR, size=14, color=SETTINGS_MUTED)
        )
        self._autoshare_config_btn.setStyleSheet(
            settings_compact_secondary_button_stylesheet(include_disabled=True)
        )
        self._autoshare_config_btn.clicked.connect(self._on_autoshare_configure)
        qc_cfg_lay.addWidget(self._autoshare_config_btn)
        as_lay.addWidget(qc_cfg_row)

        self._autoshare_expanded = saved_enabled
        self._autoshare_anim: QPropertyAnimation | None = None
        self._autoshare_container.setMaximumHeight(0 if not saved_enabled else 16777215)
        lay.addWidget(self._autoshare_container)
        self._autoshare_container.setVisible(True)
        if saved_enabled:
            self._autoshare_container.setMaximumHeight(16777215)

        return card

    def _apply_auto_share_theme(self) -> None:
        self._autoshare_icon_lbl.setPixmap(
            make_icon(ICON_SHARE_SCREEN, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        self._autoshare_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        self._autoshare_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM};"
            " background: transparent; border: none;"
        )
        self._autoshare_container.setStyleSheet(
            f"background: {SETTINGS_BG}; border: none;"
            f" border-top: 1px solid {SETTINGS_BORDER};"
        )
        self._autoshare_hotkey_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        self._autoshare_hotkey_hint.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM};"
            " background: transparent; border: none;"
        )
        self._refresh_autoshare_hotkey_label()
        if hasattr(self, "_apply_auto_key_icon_button_theme"):
            self._apply_auto_key_icon_button_theme(self._autoshare_hotkey_btn, ICON_EDIT)
        self._autoshare_separator.setStyleSheet(
            f"background: {SETTINGS_BORDER}; border: none;"
        )
        self._autoshare_crosshair_icon_lbl.setPixmap(
            make_icon(ICON_CROSSHAIR, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        self._autoshare_pos_title.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        self._autoshare_pos_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM};"
            " background: transparent; border: none;"
        )
        self._refresh_autoshare_position_label()
        self._autoshare_config_btn.setIcon(
            make_icon(ICON_CROSSHAIR, size=14, color=SETTINGS_MUTED)
        )
        self._autoshare_config_btn.setStyleSheet(
            settings_compact_secondary_button_stylesheet(include_disabled=True)
        )
        if sys.platform == "darwin" and hasattr(self, "_autoshare_access_row"):
            self._autoshare_access_row.setStyleSheet(
                f"background: {SETTINGS_SURFACE};"
                f" border: 1px solid {SETTINGS_BORDER}; border-radius: 8px;"
            )
            self._autoshare_access_title.setStyleSheet(
                f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            )
            self._autoshare_access_btn.setStyleSheet(
                settings_compact_secondary_button_stylesheet()
            )
            self._refresh_autoshare_accessibility_status()

    def _on_autoshare_toggled(self, checked):
        self._auto_share_settings.set_enabled(checked)
        self._refresh_autoshare_accessibility_status()
        self._autoshare_expanded = checked
        if self._autoshare_anim is not None:
            self._autoshare_anim.stop()
            self._autoshare_anim.deleteLater()
            self._autoshare_anim = None
        anim = QPropertyAnimation(self._autoshare_container, b"maximumHeight", self)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        current_h = self._autoshare_container.maximumHeight()
        if checked:
            self._autoshare_container.setMaximumHeight(16777215)
            target_h = self._autoshare_container.sizeHint().height()
            self._autoshare_container.setMaximumHeight(current_h)
            anim.setStartValue(current_h)
            anim.setEndValue(target_h)

            def _unlock():
                self._autoshare_container.setMaximumHeight(16777215)

            anim.finished.connect(_unlock)
        else:
            anim.setStartValue(
                current_h if current_h < 16777215 else self._autoshare_container.height()
            )
            anim.setEndValue(0)
        anim.start()
        self._autoshare_anim = anim

    def _on_autoshare_hotkey_edit(self):
        dlg = AutoKeyEditorDialog(
            self,
            sequence=self._autoshare_hotkey(),
            show_event=False,
            show_enabled=False,
            title=self.tr("Share Hotkey"),
            hint=self.tr("Press the Zoom shortcut that starts and stops screen sharing."),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_sequence:
            return
        self._auto_share_settings.set_hotkey(dlg.result_sequence)
        self._refresh_autoshare_hotkey_label()

    def _refresh_autoshare_hotkey_label(self):
        hotkey = self._autoshare_hotkey()
        self._autoshare_hotkey_value.setText(hotkey or self.tr("Not configured"))
        self._autoshare_hotkey_value.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {SETTINGS_SUCCESS if hotkey else SETTINGS_DIM};"
            " background: transparent; border: none;"
        )

    def _refresh_autoshare_position_label(self):
        saved_x, saved_y = self._auto_share_settings.click_position()
        has_pos = saved_x >= 0 and saved_y >= 0
        text = (
            self.tr("Position: {x}, {y}")
            .replace("{x}", str(saved_x))
            .replace("{y}", str(saved_y))
            if has_pos else self.tr("Not configured")
        )
        self._autoshare_pos_status.setText(text)
        self._autoshare_pos_status.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_SUCCESS if has_pos else SETTINGS_DIM};"
            " background: transparent; border: none;"
        )

    def _refresh_autoshare_accessibility_status(self):
        if sys.platform != "darwin" or not hasattr(self, "_autoshare_access_dot"):
            return
        trusted = self._autoshare_accessibility_trusted()
        color = SETTINGS_SUCCESS if trusted else SETTINGS_DANGER
        self._autoshare_access_dot.setStyleSheet(
            f"background-color: {color}; border: none; border-radius: 4px;"
        )
        if trusted:
            self._autoshare_access_desc.setText(
                self.tr("Solin can send the automatic click.")
            )
            self._autoshare_access_desc.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_SUCCESS}; background: transparent; border: none;"
            )
            self._autoshare_access_btn.setVisible(False)
        else:
            self._autoshare_access_desc.setText(
                self.tr("Allow Solin in macOS Accessibility so automatic clicks can work.")
            )
            self._autoshare_access_desc.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_DANGER}; background: transparent; border: none;"
            )
            self._autoshare_access_btn.setVisible(True)

    def _open_macos_accessibility_settings(self):
        QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))
        QTimer.singleShot(1000, self._refresh_autoshare_accessibility_status)

    def _on_autoshare_configure(self):
        from ..screen_picker_overlay import ScreenPickerOverlay

        saved_x, saved_y = self._auto_share_settings.click_position()

        self._as_overlay = ScreenPickerOverlay(
            current_x=saved_x, current_y=saved_y, parent=None,
        )
        self._as_overlay.position_picked.connect(self._on_autoshare_position_picked)
        self._as_overlay.show_overlay()

    def _on_autoshare_position_picked(self, x: int, y: int):
        self._auto_share_settings.set_click_position(x, y)
        self._refresh_autoshare_position_label()
