from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ...styles.icons import ICON_ZOOM, make_icon
from .shared import SETTINGS_DIM, SETTINGS_MUTED, SETTINGS_TEXT, SettingsToggleSwitch


class ZoomSectionMixin:
    """Builds and manages Zoom integration settings."""

    def _build_zoom_card(self):
        card, lay = self._card()

        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 10, 14, 10)
        header_lay.setSpacing(12)
        zoom_icon = QLabel()
        self._zoom_icon_lbl = zoom_icon
        zoom_icon.setPixmap(
            make_icon(ICON_ZOOM, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        zoom_icon.setFixedSize(20, 20)
        zoom_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_icon.setStyleSheet("background: transparent; border: none;")
        header_lay.addWidget(zoom_icon)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._zoom_enabled_label = QLabel(self.tr("Zoom Meetings"))
        self._zoom_enabled_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        col.addWidget(self._zoom_enabled_label)
        self._zoom_enabled_desc = QLabel(
            self.tr("Audio controls and attendance count during meetings.")
        )
        self._zoom_enabled_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        col.addWidget(self._zoom_enabled_desc)
        header_lay.addLayout(col, stretch=1)
        saved_zoom = self._zoom_settings.is_enabled()
        self._zoom_enabled_toggle = SettingsToggleSwitch(checked=saved_zoom)
        self._zoom_enabled_toggle.toggled.connect(self._on_zoom_enabled_toggled)
        header_lay.addWidget(self._zoom_enabled_toggle)
        lay.addWidget(header)

        self._zoom_settings.set_show_participants(True)
        return card

    def _apply_zoom_theme(self) -> None:
        if not hasattr(self, "_zoom_icon_lbl"):
            return
        self._zoom_icon_lbl.setPixmap(
            make_icon(ICON_ZOOM, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        self._zoom_enabled_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        self._zoom_enabled_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM};"
            " background: transparent; border: none;"
        )

    def _on_zoom_enabled_toggled(self, checked):
        self._zoom_settings.set_enabled(checked)
        self.zoom_enabled_toggled.emit(checked)

    def _on_zoom_parts_toggled(self, checked):
        self._zoom_settings.set_show_participants(checked)
        self.zoom_participants_toggled.emit(checked)
