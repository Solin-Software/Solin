from __future__ import annotations

from PySide6.QtWidgets import QLabel

from ...styles.icons import ICON_CLAPPERBOARD
from .shared import SETTINGS_WARNING_TEXT


class ScenesSectionMixin:
    """Configure the scenes engine for the next application session."""

    def _build_scenes_card(self):
        card, layout = self._card()
        enabled = self._app_settings.native_scenes_enabled()
        row, self._scenes_toggle, self._scenes_label, self._scenes_desc = self._toggle_row(
            ICON_CLAPPERBOARD,
            "",
            "",
            checked=enabled,
        )
        self._scenes_toggle.toggled.connect(self._on_scenes_toggled)
        layout.addWidget(row)

        self._scenes_restart_hint = QLabel(card)
        self._scenes_restart_hint.setWordWrap(True)
        self._bind_theme_style(
            self._scenes_restart_hint,
            lambda: (
                f"font-size: 11px; font-weight: 500; color: {SETTINGS_WARNING_TEXT};"
                " background: transparent; border: none;"
                " padding: 0 14px 10px 46px;"
            ),
        )
        self._scenes_restart_hint.setVisible(enabled != self._native_scenes_enabled)
        layout.addWidget(self._scenes_restart_hint)
        self._retranslate_scenes()
        return card

    def _on_scenes_toggled(self, enabled: bool) -> None:
        self._app_settings.set_native_scenes_enabled(enabled)
        self._scenes_restart_hint.setVisible(enabled != self._native_scenes_enabled)

    def _retranslate_scenes(self) -> None:
        if not hasattr(self, "_scenes_toggle"):
            return
        if hasattr(self, "_scenes_section_title"):
            self._scenes_section_title.setText(self.tr("Scenes").upper())
        self._scenes_label.setText(self.tr("Native scenes engine"))
        self._scenes_desc.setText(
            self.tr(
                "Disable this option if you experience problems with the native scenes engine. "
                "Solin will use classic projection and camera controls after restarting."
            )
        )
        self._scenes_restart_hint.setText(self.tr("Restart Solin to apply this change."))
