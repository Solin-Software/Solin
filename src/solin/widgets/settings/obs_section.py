from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from ...core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE
from ...core.integrations.automation.obs import OBSConnectionState
from ...styles.icons import ICON_CAST, ICON_OBS, make_icon
from ...ui.controls import NoScrollComboBox
from ...ui.obs_status_text import translated_obs_status_text
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_ACCENT_MUTED,
    SETTINGS_BG,
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DIM,
    SETTINGS_SUCCESS,
    SETTINGS_MUTED,
    SETTINGS_DANGER,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SETTINGS_WARNING_TEXT,
    SettingsToggleSwitch,
    settings_compact_secondary_button_stylesheet,
)


class ObsSectionMixin:
    """Builds and manages OBS and OBS NDI settings."""

    def _build_obs_card(self):
        card, lay = self._card()
        self._obs_last_state_message = ""

        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 10, 14, 10)
        header_lay.setSpacing(12)
        obs_icon = QLabel()
        self._obs_icon_lbl = obs_icon
        obs_icon.setPixmap(make_icon(ICON_OBS, size=18, color=SETTINGS_MUTED).pixmap(18, 18))
        obs_icon.setFixedSize(20, 20)
        obs_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        obs_icon.setStyleSheet("background: transparent; border: none;")
        header_lay.addWidget(obs_icon)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._obs_header_lbl = QLabel(self.tr("OBS Studio"))
        self._obs_header_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        col.addWidget(self._obs_header_lbl)
        self._obs_header_desc = QLabel(
            self.tr("Automatically switches scenes during projection")
        )
        self._obs_header_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        col.addWidget(self._obs_header_desc)
        header_lay.addLayout(col, stretch=1)
        obs_enabled = self._obs_settings.is_enabled()
        self._obs_toggle = SettingsToggleSwitch(checked=obs_enabled)
        self._obs_toggle.toggled.connect(self._on_obs_toggled)
        header_lay.addWidget(self._obs_toggle)
        lay.addWidget(header)

        self._obs_container = QFrame()
        self._obs_container.setStyleSheet(
            f"background: {SETTINGS_BG}; border: none;"
            f" border-top: 1px solid {SETTINGS_BORDER};"
        )
        obs_lay = QVBoxLayout(self._obs_container)
        obs_lay.setContentsMargins(14, 12, 14, 12)
        obs_lay.setSpacing(10)

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._obs_dot = QLabel("\u25cf")
        self._obs_dot.setFixedWidth(14)
        self._obs_dot.setStyleSheet(
            f"color: {SETTINGS_DIM}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_status_lbl = QLabel(self.tr("Disconnected"))
        self._obs_status_lbl.setStyleSheet(
            f"font-size: 12px; color: {SETTINGS_MUTED}; background: transparent; border: none;"
        )
        status_row.addWidget(self._obs_dot)
        status_row.addWidget(self._obs_status_lbl, stretch=1)
        obs_lay.addLayout(status_row)

        self._obs_port_lbl = QLabel(self.tr("WebSocket Port"))
        self._obs_port_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent; border: none;"
        )
        obs_lay.addWidget(self._obs_port_lbl)
        self._obs_port_edit = QLineEdit()
        self._obs_port_edit.setPlaceholderText("4455")
        saved_port = self._obs_settings.raw_websocket_port()
        if saved_port:
            self._obs_port_edit.setText(str(saved_port))
        self._obs_port_edit.setMinimumHeight(36)
        self._obs_port_edit.setStyleSheet(self._obs_field_style())
        obs_lay.addWidget(self._obs_port_edit)

        self._obs_pwd_lbl = QLabel(self.tr("Password (optional)"))
        self._obs_pwd_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent; border: none;"
        )
        obs_lay.addWidget(self._obs_pwd_lbl)
        self._obs_pwd_edit = QLineEdit()
        self._obs_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._obs_pwd_edit.setPlaceholderText(
            self.tr("Leave blank if no password is set")
        )
        saved_pwd = self._obs_settings.password()
        if saved_pwd:
            self._obs_pwd_edit.setText(saved_pwd)
        self._obs_pwd_edit.setMinimumHeight(36)
        self._obs_pwd_edit.setStyleSheet(self._obs_field_style())
        obs_lay.addWidget(self._obs_pwd_edit)

        self._obs_save_hint = QLabel(self.tr("\u25cf Changes saved automatically"))
        self._obs_save_hint.setStyleSheet(
            f"color: {SETTINGS_DIM}; font-size: 10px; background: transparent; border: none;"
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
        stream_sep.setStyleSheet(f"background: {SETTINGS_BORDER}; border: none;")
        self._obs_stream_sep = stream_sep

        stream_header = QFrame()
        stream_header.setStyleSheet("background: transparent; border: none;")
        self._obs_stream_header = stream_header
        stream_header_lay = QHBoxLayout(stream_header)
        stream_header_lay.setContentsMargins(0, 2, 0, 0)
        stream_header_lay.setSpacing(10)

        stream_icon = QLabel()
        self._obs_stream_icon_lbl = stream_icon
        stream_icon.setPixmap(make_icon(ICON_CAST, size=16, color=SETTINGS_MUTED).pixmap(16, 16))
        stream_icon.setFixedSize(20, 20)
        stream_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stream_icon.setStyleSheet("background: transparent; border: none;")
        stream_header_lay.addWidget(stream_icon)

        stream_col = QVBoxLayout()
        stream_col.setSpacing(2)
        self._obs_stream_title_lbl = QLabel(self.tr("Program stream (NDI)"))
        self._obs_stream_title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT}; background: transparent; border: none;"
        )
        stream_col.addWidget(self._obs_stream_title_lbl)
        self._obs_stream_desc_lbl = QLabel(
            self.tr("Receive the DistroAV/NDI output from OBS as a live projection.")
        )
        self._obs_stream_desc_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_desc_lbl.setWordWrap(True)
        stream_col.addWidget(self._obs_stream_desc_lbl)
        stream_header_lay.addLayout(stream_col, stretch=1)

        stream_enabled = self._obs_settings.ndi_enabled()
        self._obs_stream_toggle = SettingsToggleSwitch(checked=stream_enabled)
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
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._obs_stream_hint_lbl.setWordWrap(True)
        stream_lay.addWidget(self._obs_stream_hint_lbl)

        self._obs_stream_source_lbl = QLabel(self.tr("Available NDI sources"))
        self._obs_stream_source_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent; border: none;"
        )
        stream_lay.addWidget(self._obs_stream_source_lbl)

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self._obs_stream_sources_combo = NoScrollComboBox()
        self._obs_stream_sources_combo.setMinimumHeight(34)
        self._obs_stream_sources_combo.setStyleSheet(self._obs_combo_style())
        saved_ndi_source = self._obs_settings.ndi_source()
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
        self._obs_stream_refresh_btn.setIcon(make_icon(ICON_CAST, size=13, color=SETTINGS_MUTED))
        self._obs_stream_refresh_btn.setIconSize(QSize(13, 13))
        self._obs_stream_refresh_btn.setStyleSheet(
            settings_compact_secondary_button_stylesheet(radius=7)
        )
        self._obs_stream_refresh_btn.clicked.connect(self._refresh_obs_ndi_sources)
        source_row.addWidget(self._obs_stream_refresh_btn)
        stream_lay.addLayout(source_row)

        self._obs_stream_status_lbl = QLabel("")
        self._obs_stream_status_tone = "dim"
        self._obs_stream_status_lbl.setStyleSheet(
            self._obs_stream_status_style()
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
        sep.setStyleSheet(f"background: {SETTINGS_BORDER}; border: none;")
        self._obs_scenes_sep = sep
        scenes_lay.addWidget(sep)

        self._obs_default_lbl = QLabel(self.tr("Default scene (idle)"))
        self._obs_default_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent;"
        )
        scenes_lay.addWidget(self._obs_default_lbl)
        self._obs_default_hint = QLabel(
            self.tr("Scene shown when nothing is being projected.")
        )
        self._obs_default_hint.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent;"
        )
        self._obs_default_hint.setWordWrap(True)
        scenes_lay.addWidget(self._obs_default_hint)
        self._obs_default_combo = NoScrollComboBox()
        self._obs_default_combo.setMinimumHeight(36)
        self._obs_default_combo.setStyleSheet(self._obs_combo_style())
        self._obs_default_combo.currentTextChanged.connect(self._save_obs_scenes)
        scenes_lay.addWidget(self._obs_default_combo)

        self._obs_media_lbl = QLabel(self.tr("Media window scene"))
        self._obs_media_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; margin-top: 4px;"
        )
        scenes_lay.addWidget(self._obs_media_lbl)
        self._obs_media_hint = QLabel(self.tr(
            "Scene that captures the projection monitor. "
            "Activated when content is displayed."
        ))
        self._obs_media_hint.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent;"
        )
        self._obs_media_hint.setWordWrap(True)
        scenes_lay.addWidget(self._obs_media_hint)
        self._obs_media_combo = NoScrollComboBox()
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
            state_key = "obs:state_changed"
            if state_key not in self._theme_persistent_connections:
                self._obs.state_changed.connect(self._on_obs_state_changed)
                self._theme_persistent_connections.add(state_key)
            scenes_key = "obs:scenes_updated"
            if scenes_key not in self._theme_persistent_connections:
                self._obs.scenes_updated.connect(self._on_obs_scenes_updated)
                self._theme_persistent_connections.add(scenes_key)
            self._sync_obs_ui_state(self._obs.state, "")
        if self._ndi:
            sources_key = "ndi:sources_ready"
            if sources_key not in self._theme_persistent_connections:
                self._ndi.sources_ready.connect(self._on_obs_ndi_sources_ready)
                self._theme_persistent_connections.add(sources_key)
            error_key = "ndi:error"
            if error_key not in self._theme_persistent_connections:
                self._ndi.error.connect(self._on_obs_ndi_error)
                self._theme_persistent_connections.add(error_key)
        return card

    def _apply_obs_theme(self) -> None:
        self._obs_icon_lbl.setPixmap(
            make_icon(ICON_OBS, size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        self._obs_header_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        self._obs_header_desc.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        self._obs_container.setStyleSheet(
            f"background: {SETTINGS_BG}; border: none;"
            f" border-top: 1px solid {SETTINGS_BORDER};"
        )
        if self._obs:
            self._sync_obs_ui_state(
                self._obs.state,
                getattr(self, "_obs_last_state_message", ""),
            )
        else:
            self._obs_dot.setStyleSheet(
                f"color: {SETTINGS_DIM}; font-size: 10px; background: transparent; border: none;"
            )
            self._obs_status_lbl.setStyleSheet(
                f"font-size: 12px; color: {SETTINGS_MUTED}; background: transparent; border: none;"
            )
        for label in (
            self._obs_port_lbl,
            self._obs_pwd_lbl,
            self._obs_stream_source_lbl,
            self._obs_default_lbl,
            self._obs_media_lbl,
        ):
            label.setStyleSheet(
                f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            )
        self._obs_stream_title_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        for label in (
            self._obs_stream_desc_lbl,
            self._obs_stream_hint_lbl,
            self._obs_default_hint,
            self._obs_media_hint,
        ):
            label.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
            )
        self._obs_port_edit.setStyleSheet(self._obs_field_style())
        self._obs_pwd_edit.setStyleSheet(self._obs_field_style())
        self._obs_stream_sources_combo.setStyleSheet(self._obs_combo_style())
        self._obs_default_combo.setStyleSheet(self._obs_combo_style())
        self._obs_media_combo.setStyleSheet(self._obs_combo_style())
        self._obs_save_hint.setStyleSheet(
            f"color: {SETTINGS_DIM}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_stream_sep.setStyleSheet(f"background: {SETTINGS_BORDER}; border: none;")
        self._obs_scenes_sep.setStyleSheet(f"background: {SETTINGS_BORDER}; border: none;")
        self._obs_stream_icon_lbl.setPixmap(
            make_icon(ICON_CAST, size=16, color=SETTINGS_MUTED).pixmap(16, 16)
        )
        self._obs_stream_refresh_btn.setIcon(
            make_icon(ICON_CAST, size=13, color=SETTINGS_MUTED)
        )
        self._obs_stream_refresh_btn.setStyleSheet(
            settings_compact_secondary_button_stylesheet(radius=7)
        )
        self._obs_stream_status_lbl.setStyleSheet(self._obs_stream_status_style())

    def _obs_stream_status_style(self, tone: str | None = None) -> str:
        color = {
            "success": SETTINGS_SUCCESS,
            "danger": SETTINGS_DANGER,
            "dim": SETTINGS_DIM,
        }.get(tone or getattr(self, "_obs_stream_status_tone", "dim"), SETTINGS_DIM)
        return f"font-size: 10px; color: {color}; background: transparent; border: none;"

    def _set_obs_stream_status(self, text: str, tone: str = "dim") -> None:
        self._obs_stream_status_tone = tone
        self._obs_stream_status_lbl.setStyleSheet(self._obs_stream_status_style(tone))
        self._obs_stream_status_lbl.setText(text)

    @staticmethod
    def _obs_field_style():
        return (
            f"QLineEdit {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px;"
            f" padding: 0px 12px; font-size: 13px; }}"
            f"QLineEdit:focus {{ border-color: {SETTINGS_ACCENT}; }}"
        )

    @staticmethod
    def _obs_combo_style():
        return (
            f"QComboBox {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px;"
            f" padding: 0px 12px; font-size: 13px; }}"
            f"QComboBox:focus {{ border-color: {SETTINGS_ACCENT}; }}"
            f"QComboBox::drop-down {{ border: none; width: 28px; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px;"
            f" border-left: 4px solid transparent; border-right: 4px solid transparent;"
            f" border-top: 5px solid {SETTINGS_MUTED}; margin-right: 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; selection-background-color: {SETTINGS_ACCENT_MUTED}; }}"
        )

    def _on_obs_toggled(self, checked):
        self._obs_settings.set_enabled(checked)
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
        self._obs_settings.set_connection(port, password)
        self._obs_save_hint.setText(
            self.tr("\u2713 Configuration saved \u2014 reconnecting\u2026")
        )
        self._obs_save_hint.setStyleSheet(
            f"color: {SETTINGS_SUCCESS}; font-size: 10px; background: transparent;"
        )
        QTimer.singleShot(2500, lambda: (
            self._obs_save_hint.setText(
                self.tr("\u25cf Changes saved automatically")
            ),
            self._obs_save_hint.setStyleSheet(
                f"color: {SETTINGS_DIM}; font-size: 10px; background: transparent;"
            ),
            self._obs_save_hint.hide(),
        ))
        if self._obs and self._obs_settings.is_enabled():
            self._obs.stop()
            if port > 0:
                self._obs.start()

    def _on_obs_stream_toggled(self, checked):
        self._obs_settings.set_ndi_enabled(checked)

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
            self._set_obs_stream_status(
                self.tr("NDI receiver is not available."),
                "danger",
            )
            return
        self._obs_stream_refresh_btn.setEnabled(False)
        self._obs_stream_refresh_btn.setText(self.tr("Searching\u2026"))
        self._set_obs_stream_status(
            self.tr("Looking for NDI sources on this network."),
            "dim",
        )
        self._ndi.refresh_sources()

    def _on_obs_ndi_sources_ready(self, sources: list):
        self._obs_stream_refresh_btn.setEnabled(True)
        self._obs_stream_refresh_btn.setText(self.tr("Find sources"))
        saved = self._obs_settings.ndi_source()
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
                self._obs_settings.set_ndi_source(selected)
                self.obs_stream_config_changed.emit()
            self._set_obs_stream_status(
                self.tr("%n NDI source found.", None, len(sources)),
                "success",
            )
        else:
            self._obs_stream_sources_combo.addItem(self.tr("No NDI sources found"), "")
            self._set_obs_stream_status(
                self.tr(
                    "No NDI sources found. Check that DistroAV Main Output is enabled in OBS."
                ),
                "dim",
            )
        self._obs_stream_sources_combo.blockSignals(False)

    def _on_obs_ndi_error(self, message: str):
        if not hasattr(self, "_obs_stream_status_lbl"):
            return
        self._obs_stream_refresh_btn.setEnabled(True)
        self._obs_stream_refresh_btn.setText(self.tr("Find sources"))
        self._set_obs_stream_status(message, "danger")

    def _on_obs_stream_source_selected(self, text: str):
        data = self._obs_stream_sources_combo.currentData()
        source = data if isinstance(data, str) else text
        if source:
            self._obs_settings.set_ndi_source(source)
            self.obs_stream_config_changed.emit()

    def _save_obs_scenes(self):
        self._obs_settings.set_scenes(
            self._obs_default_combo.currentText(),
            self._obs_media_combo.currentText(),
        )
        if not MEMORIZE_PRE_MEDIA_SCENE:
            idle_set = bool(
                self._obs_default_combo.currentText()
                and not self._obs_default_combo.currentText().startswith("\u2014")
            )
            self._obs_media_combo.setEnabled(idle_set)

    def _on_obs_state_changed(self, state, message):
        self._obs_last_state_message = message or ""
        self._sync_obs_ui_state(state, message)

    def _on_obs_scenes_updated(self, scenes):
        self._populate_obs_combos(scenes)
        self._obs_scenes_frame.setVisible(bool(scenes))

    def _sync_obs_ui_state(self, state, message):
        if message:
            self._obs_last_state_message = message
        elif state != OBSConnectionState.ERROR:
            self._obs_last_state_message = ""
        dot_color = {
            OBSConnectionState.DISCONNECTED: SETTINGS_DIM,
            OBSConnectionState.CONNECTING: SETTINGS_WARNING_TEXT,
            OBSConnectionState.CONNECTED: SETTINGS_SUCCESS,
            OBSConnectionState.ERROR: SETTINGS_DANGER,
        }.get(state, SETTINGS_DIM)
        label = translated_obs_status_text(state, message)
        self._obs_dot.setStyleSheet(
            f"color: {dot_color}; font-size: 10px; background: transparent; border: none;"
        )
        self._obs_status_lbl.setStyleSheet(
            f"font-size: 12px; color: {SETTINGS_MUTED};"
            " background: transparent; border: none;"
        )
        self._obs_status_lbl.setText(label)
        if state != OBSConnectionState.CONNECTED:
            self._obs_scenes_frame.setVisible(False)

    def _populate_obs_combos(self, scenes):
        saved_default = self._obs_settings.default_scene()
        saved_media = self._obs_settings.media_window_scene()
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
        if not MEMORIZE_PRE_MEDIA_SCENE:
            idle_set = bool(saved_default and not saved_default.startswith("\u2014"))
            self._obs_media_combo.setEnabled(idle_set)

    def get_obs_default_scene(self):
        return self._obs_settings.default_scene()

    def get_obs_media_window_scene(self):
        return self._obs_settings.media_window_scene()

    def get_obs_ndi_enabled(self):
        return self._obs_settings.ndi_enabled()

    def get_obs_ndi_source(self):
        return self._obs_settings.ndi_source()
