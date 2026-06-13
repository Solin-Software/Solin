from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from ...core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE as _MEMORIZE_PRE_MEDIA
from ...core.foundation.settings_keys import SettingsKey
from ...core.integrations.automation.obs import OBSConnectionState
from ...styles.icons import ICON_CAST, ICON_OBS, make_icon
from ..common.no_scroll_combo_box import NoScrollComboBox as _NoScrollComboBox
from ._shared import (
    _ACCENT,
    _BG,
    _BORDER,
    _BORDER2,
    _DIM,
    _GREEN,
    _MUTED,
    _RED,
    _SURF,
    _TEXT,
    _ToggleSwitch,
)


class ObsSectionMixin:
    """Builds and manages OBS and OBS NDI settings."""

    def _build_obs_card(self):
        card, lay = self._card()

        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 10, 14, 10)
        header_lay.setSpacing(12)
        obs_icon = QLabel()
        obs_icon.setPixmap(make_icon(ICON_OBS, size=18, color=_MUTED).pixmap(18, 18))
        obs_icon.setFixedSize(20, 20)
        obs_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        obs_icon.setStyleSheet("background: transparent; border: none;")
        header_lay.addWidget(obs_icon)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._obs_header_lbl = QLabel(self.tr("OBS Studio"))
        self._obs_header_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        col.addWidget(self._obs_header_lbl)
        self._obs_header_desc = QLabel(
            self.tr("Automatically switches scenes during projection")
        )
        self._obs_header_desc.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        col.addWidget(self._obs_header_desc)
        header_lay.addLayout(col, stretch=1)
        obs_enabled = self._prefs.value(SettingsKey.OBS_ENABLED, False, bool)
        self._obs_toggle = _ToggleSwitch(checked=obs_enabled)
        self._obs_toggle.toggled.connect(self._on_obs_toggled)
        header_lay.addWidget(self._obs_toggle)
        lay.addWidget(header)

        self._obs_container = QFrame()
        self._obs_container.setStyleSheet(
            f"background: {_BG}; border: none;"
            f" border-top: 1px solid {_BORDER};"
        )
        obs_lay = QVBoxLayout(self._obs_container)
        obs_lay.setContentsMargins(14, 12, 14, 12)
        obs_lay.setSpacing(10)

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._obs_dot = QLabel("\u25cf")
        self._obs_dot.setFixedWidth(14)
        self._obs_dot.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_status_lbl = QLabel(self.tr("Disconnected"))
        self._obs_status_lbl.setStyleSheet(
            f"font-size: 12px; color: {_MUTED}; background: transparent; border: none;"
        )
        status_row.addWidget(self._obs_dot)
        status_row.addWidget(self._obs_status_lbl, stretch=1)
        obs_lay.addLayout(status_row)

        self._obs_port_lbl = QLabel(self.tr("WebSocket Port"))
        self._obs_port_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {_TEXT}; background: transparent; border: none;"
        )
        obs_lay.addWidget(self._obs_port_lbl)
        self._obs_port_edit = QLineEdit()
        self._obs_port_edit.setPlaceholderText("4455")
        saved_port = self._prefs.value(SettingsKey.OBS_PORT, "", str)
        if saved_port:
            self._obs_port_edit.setText(str(saved_port))
        self._obs_port_edit.setMinimumHeight(36)
        self._obs_port_edit.setStyleSheet(self._obs_field_style())
        obs_lay.addWidget(self._obs_port_edit)

        self._obs_pwd_lbl = QLabel(self.tr("Password (optional)"))
        self._obs_pwd_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {_TEXT}; background: transparent; border: none;"
        )
        obs_lay.addWidget(self._obs_pwd_lbl)
        self._obs_pwd_edit = QLineEdit()
        self._obs_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._obs_pwd_edit.setPlaceholderText(
            self.tr("Leave blank if no password is set")
        )
        saved_pwd = self._prefs.value(SettingsKey.OBS_PASSWORD, "", str)
        if saved_pwd:
            self._obs_pwd_edit.setText(saved_pwd)
        self._obs_pwd_edit.setMinimumHeight(36)
        self._obs_pwd_edit.setStyleSheet(self._obs_field_style())
        obs_lay.addWidget(self._obs_pwd_edit)

        self._obs_save_hint = QLabel(self.tr("\u25cf Changes saved automatically"))
        self._obs_save_hint.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_save_hint.hide()
        obs_lay.addWidget(self._obs_save_hint)

        self._obs_save_timer = QTimer(self)
        self._obs_save_timer.setSingleShot(True)
        self._obs_save_timer.setInterval(800)
        self._obs_save_timer.timeout.connect(self._save_obs_config)
        self._obs_port_edit.textChanged.connect(self._obs_field_changed)
        self._obs_pwd_edit.textChanged.connect(self._obs_field_changed)

        stream_sep = QFrame()
        stream_sep.setFixedHeight(1)
        stream_sep.setStyleSheet(f"background: {_BORDER}; border: none;")
        self._obs_stream_sep = stream_sep

        stream_header = QFrame()
        stream_header.setStyleSheet("background: transparent; border: none;")
        self._obs_stream_header = stream_header
        stream_header_lay = QHBoxLayout(stream_header)
        stream_header_lay.setContentsMargins(0, 2, 0, 0)
        stream_header_lay.setSpacing(10)

        stream_icon = QLabel()
        stream_icon.setPixmap(make_icon(ICON_CAST, size=16, color=_MUTED).pixmap(16, 16))
        stream_icon.setFixedSize(20, 20)
        stream_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stream_icon.setStyleSheet("background: transparent; border: none;")
        stream_header_lay.addWidget(stream_icon)

        stream_col = QVBoxLayout()
        stream_col.setSpacing(2)
        self._obs_stream_title_lbl = QLabel(self.tr("Program stream (NDI)"))
        self._obs_stream_title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {_TEXT}; background: transparent; border: none;"
        )
        stream_col.addWidget(self._obs_stream_title_lbl)
        self._obs_stream_desc_lbl = QLabel(
            self.tr("Receive the DistroAV/NDI output from OBS as a live projection.")
        )
        self._obs_stream_desc_lbl.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_desc_lbl.setWordWrap(True)
        stream_col.addWidget(self._obs_stream_desc_lbl)
        stream_header_lay.addLayout(stream_col, stretch=1)

        stream_enabled = self._prefs.value(SettingsKey.OBS_NDI_ENABLED, False, bool)
        self._obs_stream_toggle = _ToggleSwitch(checked=stream_enabled)
        self._obs_stream_toggle.toggled.connect(self._on_obs_stream_toggled)
        stream_header_lay.addWidget(self._obs_stream_toggle)

        self._obs_stream_container = QFrame()
        self._obs_stream_container.setStyleSheet("background: transparent; border: none;")
        stream_lay = QVBoxLayout(self._obs_stream_container)
        stream_lay.setContentsMargins(30, 0, 0, 0)
        stream_lay.setSpacing(8)

        self._obs_stream_hint_lbl = QLabel(
            self.tr("Enable Main Output in DistroAV, then select the NDI source shown by OBS.")
        )
        self._obs_stream_hint_lbl.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_hint_lbl.setWordWrap(True)
        stream_lay.addWidget(self._obs_stream_hint_lbl)

        self._obs_stream_source_lbl = QLabel(self.tr("Available NDI sources"))
        self._obs_stream_source_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {_TEXT}; background: transparent; border: none;"
        )
        stream_lay.addWidget(self._obs_stream_source_lbl)

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self._obs_stream_sources_combo = _NoScrollComboBox()
        self._obs_stream_sources_combo.setMinimumHeight(34)
        self._obs_stream_sources_combo.setStyleSheet(self._obs_combo_style())
        saved_ndi_source = self._prefs.value(SettingsKey.OBS_NDI_SOURCE, "", str)
        if saved_ndi_source:
            self._obs_stream_sources_combo.addItem(saved_ndi_source, saved_ndi_source)
        else:
            self._obs_stream_sources_combo.addItem(self.tr("No sources loaded"), "")
        self._obs_stream_sources_combo.currentTextChanged.connect(
            self._on_obs_stream_source_selected
        )
        source_row.addWidget(self._obs_stream_sources_combo, stretch=1)

        self._obs_stream_refresh_btn = QPushButton(self.tr("Find sources"))
        self._obs_stream_refresh_btn.setFixedHeight(34)
        self._obs_stream_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._obs_stream_refresh_btn.setIcon(make_icon(ICON_CAST, size=13, color=_MUTED))
        self._obs_stream_refresh_btn.setIconSize(QSize(13, 13))
        self._obs_stream_refresh_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {_BORDER2}; border-radius: 7px;"
            f" background: {_BORDER}; color: #c9d1d9; font-size: 11px; padding: 0 10px; }}"
            f"QPushButton:hover {{ background: {_BORDER2}; }}"
        )
        self._obs_stream_refresh_btn.clicked.connect(self._refresh_obs_ndi_sources)
        source_row.addWidget(self._obs_stream_refresh_btn)
        stream_lay.addLayout(source_row)

        self._obs_stream_status_lbl = QLabel("")
        self._obs_stream_status_lbl.setStyleSheet(
            f"font-size: 10px; color: {_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_status_lbl.setWordWrap(True)
        stream_lay.addWidget(self._obs_stream_status_lbl)

        self._obs_scenes_frame = QFrame()
        self._obs_scenes_frame.setVisible(False)
        self._obs_scenes_frame.setStyleSheet("background: transparent; border: none;")
        scenes_lay = QVBoxLayout(self._obs_scenes_frame)
        scenes_lay.setContentsMargins(0, 6, 0, 0)
        scenes_lay.setSpacing(8)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {_BORDER}; border: none;")
        scenes_lay.addWidget(sep)

        self._obs_default_lbl = QLabel(self.tr("Default scene (idle)"))
        self._obs_default_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {_TEXT}; background: transparent;"
        )
        scenes_lay.addWidget(self._obs_default_lbl)
        self._obs_default_hint = QLabel(
            self.tr("Scene shown when nothing is being projected.")
        )
        self._obs_default_hint.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent;"
        )
        self._obs_default_hint.setWordWrap(True)
        scenes_lay.addWidget(self._obs_default_hint)
        self._obs_default_combo = _NoScrollComboBox()
        self._obs_default_combo.setMinimumHeight(36)
        self._obs_default_combo.setStyleSheet(self._obs_combo_style())
        self._obs_default_combo.currentTextChanged.connect(self._save_obs_scenes)
        scenes_lay.addWidget(self._obs_default_combo)

        self._obs_media_lbl = QLabel(self.tr("Media window scene"))
        self._obs_media_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {_TEXT};"
            " background: transparent; margin-top: 4px;"
        )
        scenes_lay.addWidget(self._obs_media_lbl)
        self._obs_media_hint = QLabel(self.tr(
            "Scene that captures the projection monitor. "
            "Activated when content is displayed."
        ))
        self._obs_media_hint.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent;"
        )
        self._obs_media_hint.setWordWrap(True)
        scenes_lay.addWidget(self._obs_media_hint)
        self._obs_media_combo = _NoScrollComboBox()
        self._obs_media_combo.setMinimumHeight(36)
        self._obs_media_combo.setStyleSheet(self._obs_combo_style())
        self._obs_media_combo.currentTextChanged.connect(self._save_obs_scenes)
        scenes_lay.addWidget(self._obs_media_combo)

        obs_lay.addWidget(self._obs_scenes_frame)
        obs_lay.addWidget(self._obs_stream_sep)
        obs_lay.addWidget(self._obs_stream_header)
        obs_lay.addWidget(self._obs_stream_container)
        self._obs_stream_container.setMaximumHeight(16777215 if stream_enabled else 0)
        self._obs_stream_anim: QPropertyAnimation | None = None
        self._obs_expanded = obs_enabled
        self._obs_anim: QPropertyAnimation | None = None
        self._obs_container.setMaximumHeight(0 if not obs_enabled else 16777215)
        lay.addWidget(self._obs_container)
        self._obs_container.setVisible(True)
        if obs_enabled:
            self._obs_container.setMaximumHeight(16777215)

        if self._obs:
            self._obs.state_changed.connect(self._on_obs_state_changed)
            self._obs.scenes_updated.connect(self._on_obs_scenes_updated)
            self._sync_obs_ui_state(self._obs.state, "")
        if self._ndi:
            self._ndi.sources_ready.connect(self._on_obs_ndi_sources_ready)
            self._ndi.error.connect(self._on_obs_ndi_error)
        return card

    @staticmethod
    def _obs_field_style():
        return (
            f"QLineEdit {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; border-radius: 8px;"
            f" padding: 0px 12px; font-size: 13px; }}"
            f"QLineEdit:focus {{ border-color: {_ACCENT}; }}"
        )

    @staticmethod
    def _obs_combo_style():
        return (
            f"QComboBox {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; border-radius: 8px;"
            f" padding: 0px 12px; font-size: 13px; }}"
            f"QComboBox:focus {{ border-color: {_ACCENT}; }}"
            f"QComboBox::drop-down {{ border: none; width: 28px; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px;"
            f" border-left: 4px solid transparent; border-right: 4px solid transparent;"
            f" border-top: 5px solid {_MUTED}; margin-right: 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; selection-background-color: #1f3a6e; }}"
        )

    def _on_obs_toggled(self, checked):
        self._prefs.setValue(SettingsKey.OBS_ENABLED, checked)
        self._obs_expanded = checked
        if self._obs_anim is not None:
            self._obs_anim.stop()
            self._obs_anim.deleteLater()
            self._obs_anim = None
        anim = QPropertyAnimation(self._obs_container, b"maximumHeight", self)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        current_h = self._obs_container.maximumHeight()
        if checked:
            self._obs_container.setMaximumHeight(16777215)
            target_h = self._obs_container.sizeHint().height()
            self._obs_container.setMaximumHeight(current_h)
            anim.setStartValue(current_h)
            anim.setEndValue(target_h)

            def _unlock_obs():
                self._obs_container.setMaximumHeight(16777215)

            anim.finished.connect(_unlock_obs)
        else:
            anim.setStartValue(
                current_h if current_h < 16777215 else self._obs_container.height()
            )
            anim.setEndValue(0)
        anim.start()
        self._obs_anim = anim
        if self._obs:
            if checked:
                self._obs.start()
            else:
                self._obs.stop()

    def _obs_field_changed(self):
        self._obs_save_hint.show()
        self._obs_save_timer.start()

    def _save_obs_config(self):
        port_text = self._obs_port_edit.text().strip()
        password = self._obs_pwd_edit.text()
        try:
            port = int(port_text) if port_text else 0
        except ValueError:
            port = 0
        self._prefs.setValue(SettingsKey.OBS_PORT, port)
        self._prefs.setValue(SettingsKey.OBS_PASSWORD, password)
        self._obs_save_hint.setText(
            self.tr("\u2713 Configuration saved \u2014 reconnecting\u2026")
        )
        self._obs_save_hint.setStyleSheet(
            f"color: {_GREEN}; font-size: 10px; background: transparent;"
        )
        QTimer.singleShot(2500, lambda: (
            self._obs_save_hint.setText(
                self.tr("\u25cf Changes saved automatically")
            ),
            self._obs_save_hint.setStyleSheet(
                f"color: {_DIM}; font-size: 10px; background: transparent;"
            ),
            self._obs_save_hint.hide(),
        ))
        if self._obs and self._prefs.value(SettingsKey.OBS_ENABLED, False, bool):
            self._obs.stop()
            if port > 0:
                self._obs.start()

    def _on_obs_stream_toggled(self, checked):
        self._prefs.setValue(SettingsKey.OBS_NDI_ENABLED, checked)

        if self._obs_stream_anim is not None:
            self._obs_stream_anim.stop()
            self._obs_stream_anim.deleteLater()
            self._obs_stream_anim = None

        anim = QPropertyAnimation(self._obs_stream_container, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        current_h = self._obs_stream_container.maximumHeight()
        if checked:
            self._obs_stream_container.setMaximumHeight(16777215)
            target_h = self._obs_stream_container.sizeHint().height()
            self._obs_stream_container.setMaximumHeight(current_h)
            anim.setStartValue(current_h)
            anim.setEndValue(target_h)

            def _unlock_stream():
                self._obs_stream_container.setMaximumHeight(16777215)

            anim.finished.connect(_unlock_stream)
        else:
            anim.setStartValue(
                current_h if current_h < 16777215 else self._obs_stream_container.height()
            )
            anim.setEndValue(0)
        anim.start()
        self._obs_stream_anim = anim

        if checked and self._obs_stream_sources_combo.count() <= 1:
            self._refresh_obs_ndi_sources()
        self.obs_stream_config_changed.emit()

    def _refresh_obs_ndi_sources(self):
        if not self._ndi:
            self._obs_stream_status_lbl.setText(self.tr("NDI receiver is not available."))
            return
        self._obs_stream_refresh_btn.setEnabled(False)
        self._obs_stream_refresh_btn.setText(self.tr("Searching\u2026"))
        self._obs_stream_status_lbl.setStyleSheet(
            f"font-size: 10px; color: {_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_status_lbl.setText(self.tr("Looking for NDI sources on this network."))
        self._ndi.refresh_sources()

    def _on_obs_ndi_sources_ready(self, sources: list):
        self._obs_stream_refresh_btn.setEnabled(True)
        self._obs_stream_refresh_btn.setText(self.tr("Find sources"))
        saved = self._prefs.value(SettingsKey.OBS_NDI_SOURCE, "", str)
        self._obs_stream_sources_combo.blockSignals(True)
        self._obs_stream_sources_combo.clear()
        if sources:
            for source in sources:
                self._obs_stream_sources_combo.addItem(source, source)
            selected_idx = 0
            if saved:
                idx = self._obs_stream_sources_combo.findText(saved)
                if idx >= 0:
                    selected_idx = idx
            self._obs_stream_sources_combo.setCurrentIndex(selected_idx)
            selected = self._obs_stream_sources_combo.currentData()
            if isinstance(selected, str) and selected:
                self._prefs.setValue(SettingsKey.OBS_NDI_SOURCE, selected)
                self.obs_stream_config_changed.emit()
            self._obs_stream_status_lbl.setStyleSheet(
                f"font-size: 10px; color: {_GREEN}; background: transparent; border: none;"
            )
            self._obs_stream_status_lbl.setText(
                self.tr("%n NDI source found.", None, len(sources))
            )
        else:
            self._obs_stream_sources_combo.addItem(self.tr("No NDI sources found"), "")
            self._obs_stream_status_lbl.setStyleSheet(
                f"font-size: 10px; color: {_DIM}; background: transparent; border: none;"
            )
            self._obs_stream_status_lbl.setText(
                self.tr("No NDI sources found. Check that DistroAV Main Output is enabled in OBS.")
            )
        self._obs_stream_sources_combo.blockSignals(False)

    def _on_obs_ndi_error(self, message: str):
        if not hasattr(self, "_obs_stream_status_lbl"):
            return
        self._obs_stream_refresh_btn.setEnabled(True)
        self._obs_stream_refresh_btn.setText(self.tr("Find sources"))
        self._obs_stream_status_lbl.setStyleSheet(
            f"font-size: 10px; color: {_RED}; background: transparent; border: none;"
        )
        self._obs_stream_status_lbl.setText(message)

    def _on_obs_stream_source_selected(self, text: str):
        data = self._obs_stream_sources_combo.currentData()
        source = data if isinstance(data, str) else text
        if source:
            self._prefs.setValue(SettingsKey.OBS_NDI_SOURCE, source)
            self.obs_stream_config_changed.emit()

    def _save_obs_scenes(self):
        self._prefs.setValue(
            SettingsKey.OBS_DEFAULT_SCENE, self._obs_default_combo.currentText()
        )
        self._prefs.setValue(
            SettingsKey.OBS_MEDIA_WINDOW_SCENE, self._obs_media_combo.currentText()
        )
        if not _MEMORIZE_PRE_MEDIA:
            idle_set = bool(
                self._obs_default_combo.currentText()
                and not self._obs_default_combo.currentText().startswith("\u2014")
            )
            self._obs_media_combo.setEnabled(idle_set)

    def _on_obs_state_changed(self, state, message):
        self._sync_obs_ui_state(state, message)

    def _on_obs_scenes_updated(self, scenes):
        self._populate_obs_combos(scenes)
        self._obs_scenes_frame.setVisible(bool(scenes))

    def _sync_obs_ui_state(self, state, message):
        dot_color = {
            OBSConnectionState.DISCONNECTED: _DIM,
            OBSConnectionState.CONNECTING: "#e3b341",
            OBSConnectionState.CONNECTED: _GREEN,
            OBSConnectionState.ERROR: _RED,
        }.get(state, _DIM)
        label = {
            OBSConnectionState.DISCONNECTED: self.tr("Disconnected"),
            OBSConnectionState.CONNECTING: self.tr("Connecting\u2026"),
            OBSConnectionState.CONNECTED: self.tr("Connected to OBS Studio"),
            OBSConnectionState.ERROR: (
                self.tr("Error: {msg}").replace("{msg}", message)
                if message else self.tr("Connection error")
            ),
        }.get(state, message or self.tr("Disconnected"))
        self._obs_dot.setStyleSheet(
            f"color: {dot_color}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_status_lbl.setText(label)
        if state != OBSConnectionState.CONNECTED:
            self._obs_scenes_frame.setVisible(False)

    def _populate_obs_combos(self, scenes):
        saved_default = self._prefs.value(SettingsKey.OBS_DEFAULT_SCENE, "", str)
        saved_media = self._prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)
        for combo, saved in [
            (self._obs_default_combo, saved_default),
            (self._obs_media_combo, saved_media),
        ]:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.tr("\u2014 Select scene \u2014"), "")
            for scene in scenes:
                combo.addItem(scene, scene)
            if saved:
                idx = combo.findText(saved)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        if not _MEMORIZE_PRE_MEDIA:
            idle_set = bool(saved_default and not saved_default.startswith("\u2014"))
            self._obs_media_combo.setEnabled(idle_set)

    def get_obs_default_scene(self):
        return self._prefs.value(SettingsKey.OBS_DEFAULT_SCENE, "", str)

    def get_obs_media_window_scene(self):
        return self._prefs.value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "", str)

    def get_obs_ndi_enabled(self):
        return self._prefs.value(SettingsKey.OBS_NDI_ENABLED, False, bool)

    def get_obs_ndi_source(self):
        return self._prefs.value(SettingsKey.OBS_NDI_SOURCE, "", str)
