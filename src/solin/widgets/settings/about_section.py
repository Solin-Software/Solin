from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...core.foundation.constants import APP_VERSION
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_ACCENT_HOVER,
    SETTINGS_BORDER,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_TEXT,
)


class AboutSectionMixin:
    """Builds the about/settings metadata section."""

    def _build_about_card(self):
        card, lay = self._card()
        inner = QFrame()
        inner.setStyleSheet("background: transparent; border: none;")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(14, 12, 14, 12)
        inner_lay.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        name_lbl = QLabel("Solin")
        self._about_name_lbl = name_lbl
        name_lbl.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT}; background: transparent;"
        )
        header_row.addWidget(name_lbl)
        ver_badge = QLabel(APP_VERSION)
        self._about_ver_badge = ver_badge
        ver_badge.setStyleSheet(
            f"font-size: 10px; color: {SETTINGS_MUTED}; background: {SETTINGS_BORDER};"
            " border-radius: 4px; padding: 2px 6px; font-weight: 600;"
        )
        header_row.addWidget(ver_badge)
        header_row.addStretch()
        inner_lay.addLayout(header_row)

        self._about_desc_lbl = QLabel(
            self.tr("Audio & Video app for Kingdom Hall meetings.")
        )
        self._about_desc_lbl.setStyleSheet(
            f"font-size: 12px; color: {SETTINGS_MUTED}; background: transparent;"
        )
        self._about_desc_lbl.setWordWrap(True)
        inner_lay.addWidget(self._about_desc_lbl)

        links_row = QHBoxLayout()
        links_row.setSpacing(16)
        self._link_site_btn = QPushButton(self.tr("Official Website"))
        self._link_site_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._link_site_btn.setStyleSheet(self._about_link_button_style())
        self._link_site_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://solinav.vercel.app/"))
        )
        links_row.addWidget(self._link_site_btn)
        self._link_changelog_btn = QPushButton(self.tr("Changelog"))
        self._link_changelog_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._link_changelog_btn.setStyleSheet(self._about_link_button_style())
        self._link_changelog_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://solinav.vercel.app/changelog")
            )
        )
        links_row.addWidget(self._link_changelog_btn)
        links_row.addStretch()
        inner_lay.addLayout(links_row)
        inner_lay.addSpacing(4)

        self._disclaimer_lbl = QLabel(self.tr(
            "This app is independent and is not affiliated with or endorsed by "
            "the Watch Tower Bible and Tract Society of Pennsylvania or any of "
            "its associated organizations."
        ))
        self._disclaimer_lbl.setStyleSheet(
            f"font-size: 10px; color: {SETTINGS_DIM}; background: transparent;"
        )
        self._disclaimer_lbl.setWordWrap(True)
        inner_lay.addWidget(self._disclaimer_lbl)
        lay.addWidget(inner)
        return card

    @staticmethod
    def _about_link_button_style() -> str:
        return (
            f"QPushButton {{ color: {SETTINGS_ACCENT}; font-size: 12px;"
            " font-weight: 500; background: transparent; border: none;"
            " padding: 0px; }"
            f"QPushButton:hover {{ color: {SETTINGS_ACCENT_HOVER}; }}"
        )

    def _apply_about_theme(self) -> None:
        self._about_name_lbl.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT};"
            " background: transparent;"
        )
        self._about_ver_badge.setStyleSheet(
            f"font-size: 10px; color: {SETTINGS_MUTED};"
            f" background: {SETTINGS_BORDER}; border-radius: 4px;"
            " padding: 2px 6px; font-weight: 600;"
        )
        self._about_desc_lbl.setStyleSheet(
            f"font-size: 12px; color: {SETTINGS_MUTED}; background: transparent;"
        )
        self._link_site_btn.setStyleSheet(self._about_link_button_style())
        self._link_changelog_btn.setStyleSheet(self._about_link_button_style())
        self._disclaimer_lbl.setStyleSheet(
            f"font-size: 10px; color: {SETTINGS_DIM}; background: transparent;"
        )
