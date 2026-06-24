from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ...ui.screens import ScreenManager
from ...styles.icons import ICON_MONITOR, ICON_TV, make_icon
from .shared import (
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_SUCCESS,
    SETTINGS_TEXT,
    SETTINGS_TEXT_ON_ACCENT,
)


class ScreensSectionMixin:
    """Builds and refreshes the screen topology settings section."""

    def _populate_screens(self):
        lay = self._screens_card_lay
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        primary = ScreenManager.primary_screen()
        secondary = ScreenManager.secondary_screens()

        p_row = QFrame()
        p_row.setStyleSheet("background: transparent; border: none;")
        p_lay = QHBoxLayout(p_row)
        p_lay.setContentsMargins(14, 10, 14, 10)
        p_lay.setSpacing(10)
        p_icon = QLabel()
        p_icon.setPixmap(
            make_icon(ICON_MONITOR, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        p_icon.setFixedSize(20, 20)
        p_icon.setStyleSheet("background: transparent; border: none;")
        p_lay.addWidget(p_icon)
        p_col = QVBoxLayout()
        p_col.setSpacing(1)
        p_title = QLabel(self.tr("Primary Screen (control)"))
        p_title.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        p_col.addWidget(p_title)
        p_geo = primary.geometry()
        p_res = QLabel(
            f"{p_geo.width()} \u00d7 {p_geo.height()} px  \u2022  {primary.name()}"
        )
        p_res.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_MUTED}; background: transparent; border: none;"
        )
        p_col.addWidget(p_res)
        p_lay.addLayout(p_col, stretch=1)
        lay.addWidget(p_row)

        if not secondary:
            lay.addWidget(self._divider())
            no_lbl = QLabel(
                self.tr("No secondary screen detected. Connect an external monitor.")
            )
            no_lbl.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_DIM}; padding: 8px 14px;"
                " background: transparent; border: none;"
            )
            no_lbl.setWordWrap(True)
            lay.addWidget(no_lbl)
            return

        for i, screen in enumerate(secondary, 1):
            lay.addWidget(self._divider())
            s_row = QFrame()
            s_row.setStyleSheet("background: transparent; border: none;")
            s_lay_h = QHBoxLayout(s_row)
            s_lay_h.setContentsMargins(14, 10, 14, 10)
            s_lay_h.setSpacing(10)
            s_icon = QLabel()
            s_icon.setPixmap(
                make_icon(ICON_TV, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
            )
            s_icon.setFixedSize(20, 20)
            s_icon.setStyleSheet("background: transparent; border: none;")
            s_lay_h.addWidget(s_icon)
            s_col = QVBoxLayout()
            s_col.setSpacing(1)
            s_title = QLabel(
                self.tr("Secondary {n} (projection)").replace("{n}", str(i))
            )
            s_title.setStyleSheet(
                f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            )
            s_col.addWidget(s_title)
            geo = screen.geometry()
            s_res = QLabel(
                f"{geo.width()} \u00d7 {geo.height()} px"
                f"  \u2022  {screen.name()}"
            )
            s_res.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_MUTED};"
                " background: transparent; border: none;"
            )
            s_col.addWidget(s_res)
            s_lay_h.addLayout(s_col, stretch=1)
            badge = QLabel(self.tr("PROJECTION"))
            badge.setStyleSheet(
                f"background: {SETTINGS_SUCCESS}; border-radius: 4px;"
                f" padding: 2px 6px; color: {SETTINGS_TEXT_ON_ACCENT};"
                " font-size: 10px; font-weight: 700;"
            )
            s_lay_h.addWidget(badge)
            lay.addWidget(s_row)

    def _refresh_screens(self):
        self._populate_screens()
