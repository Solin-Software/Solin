"""OBS setup lifecycle mixin for ProfileScreen onboarding."""
from __future__ import annotations

from PySide6.QtCore import QTimer

from ..core.integrations.automation.obs import OBSConnectionState
from .profile_widgets import _AMBER, _DIM, _GREEN, _RED


class ProfileOBSSetupMixin:
    def _init_ob_obs_setup(self) -> None:
        self._ob_obs_last_state = OBSConnectionState.DISCONNECTED
        self._ob_obs_last_message = ""
        self._obs_probe.state_changed.connect(self._ob_obs_on_state)
        self._obs_probe.scenes_updated.connect(self._ob_obs_on_scenes)
        self._ob_obs_reconnect_timer = QTimer(self)
        self._ob_obs_reconnect_timer.setSingleShot(True)
        self._ob_obs_reconnect_timer.setInterval(800)
        self._ob_obs_reconnect_timer.timeout.connect(self._ob_obs_do_reconnect)

    # ── OBS service lifecycle ──────────────────────────────────────────────

    def _ob_obs_teardown(self) -> None:
        """Stop and discard the onboarding OBS service, cancel pending reconnect."""
        self._ob_obs_reconnect_timer.stop()
        self._obs_probe.stop()

    def _ob_obs_start(self) -> None:
        """Apply current port/password to prefs then start the service."""
        port_text = self._ob_obs_port.text().strip()
        try:
            port = int(port_text) if port_text else 4455
        except ValueError:
            port = 4455
        pwd = self._ob_obs_pwd.text()

        if port > 0:
            self._obs_probe.connect_to(port, pwd)

    def _ob_obs_do_reconnect(self) -> None:
        """Debounced slot: reconnect after field changes."""
        if self._ob_obs_toggle.is_checked:
            self._ob_obs_start()

    # ── OBS field / toggle handlers ────────────────────────────────────────

    def _on_ob_obs_toggled(self, checked: bool) -> None:
        if checked:
            self._ob_obs_fields.show()
            self._ob_obs_start()
        else:
            self._ob_obs_fields.hide()
            self._ob_obs_teardown()
            self._ob_obs_scenes_frame.setVisible(False)

    def _ob_obs_field_changed(self) -> None:
        """Debounce reconnect on port/password edits."""
        if self._ob_obs_toggle.is_checked:
            self._ob_obs_reconnect_timer.start()

    # ── OBS signal handlers ────────────────────────────────────────────────

    def _ob_obs_on_state(self, state: OBSConnectionState, message: str) -> None:
        self._ob_obs_last_state = state
        self._ob_obs_last_message = message
        self._refresh_ob_obs_status()

        if state != OBSConnectionState.CONNECTED:
            self._ob_obs_scenes_frame.setVisible(False)

    def _refresh_ob_obs_status(self) -> None:
        if not hasattr(self, "_ob_obs_status_lbl"):
            return
        state = getattr(self, "_ob_obs_last_state", OBSConnectionState.DISCONNECTED)
        message = getattr(self, "_ob_obs_last_message", "")
        dot_color = {
            OBSConnectionState.DISCONNECTED: _DIM,
            OBSConnectionState.CONNECTING:   _AMBER,
            OBSConnectionState.CONNECTED:    _GREEN,
            OBSConnectionState.ERROR:        _RED,
        }.get(state, _DIM)
        label = {
            OBSConnectionState.DISCONNECTED: self.tr("Disconnected"),
            OBSConnectionState.CONNECTING:   self.tr("Connecting\u2026"),
            OBSConnectionState.CONNECTED:    self.tr("Connected to OBS Studio"),
            OBSConnectionState.ERROR: (
                self.tr("Error: {msg}").replace("{msg}", message)
                if message else self.tr("Connection error")
            ),
        }.get(state, message or self.tr("Disconnected"))

        self._ob_obs_status_dot.setStyleSheet(
            f"color: {dot_color}; font-size: 10px;"
            " background: transparent; border: none;"
        )
        self._ob_obs_status_lbl.setText(label)

    def _ob_obs_on_scenes(self, scenes: list[str]) -> None:
        self._ob_obs_populate_combos(scenes)
        self._ob_obs_scenes_frame.setVisible(bool(scenes))

    def _ob_obs_populate_combos(self, scenes: list[str]) -> None:
        placeholder = self.tr("\u2014 Select scene \u2014")
        for combo in (self._ob_obs_default_combo, self._ob_obs_media_combo):
            selected = combo.currentData() or combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, "")
            for s in scenes:
                combo.addItem(s, s)
            idx = combo.findData(selected)
            if idx < 0 and selected:
                idx = combo.findText(selected)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.blockSignals(False)
