from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ...core.remote_control.network import (
    LanInterface,
    discover_lan_interfaces,
)
from ...core.remote_control.security import (
    PASSWORD_MIN_LENGTH,
    PasswordValidationError,
    UsernameValidationError,
)
from ...core.remote_control.settings import REMOTE_CONTROL_PORT
from ...styles.icons import ICON_CHECK, ICON_COPY, ICON_REMOTE_CONTROL, make_icon
from ...styles.theme import PALETTE
from ...ui.controls import ButtonConfirmationFeedback, NoScrollComboBox
from ..remote_control_setup_dialog import (
    RemoteControlSetupDialog,
    RemoteControlSetupPresentation,
)
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DANGER,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_SUCCESS,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SETTINGS_TEXT_SECONDARY,
    settings_compact_secondary_button_stylesheet,
    settings_picker_primary_button_stylesheet,
)


class RemoteControlSectionMixin:
    """Settings card for the profile-scoped HTTPS LAN remote controller."""

    def _build_remote_control_card(self):
        card, layout = self._card()
        enabled_row, toggle, title, description = self._toggle_row(
            ICON_REMOTE_CONTROL,
            self.tr("Remote control"),
            self.tr("Control Solin securely from another device on this local network."),
            self._remote_control_settings.enabled(),
        )
        self._remote_control_toggle = toggle
        self._remote_control_title = title
        self._remote_control_description = description
        toggle.toggled.connect(self._on_remote_control_toggled)
        layout.addWidget(enabled_row)
        layout.addWidget(self._divider())

        configuration = QFrame()
        configuration.setStyleSheet("background: transparent; border: none;")
        configuration_layout = QVBoxLayout(configuration)
        configuration_layout.setContentsMargins(14, 12, 14, 14)
        configuration_layout.setSpacing(10)

        self._remote_network_label = QLabel(self.tr("Network interface"))
        self._bind_theme_style(self._remote_network_label, self._remote_field_label_style)
        configuration_layout.addWidget(self._remote_network_label)

        self._remote_interface_combo = NoScrollComboBox(configuration)
        self._remote_interface_combo.setFixedHeight(34)
        self._bind_theme_style(self._remote_interface_combo, self._remote_field_style)
        self._remote_interfaces: dict[str, LanInterface] = {}
        self._populate_remote_interfaces()
        self._remote_interface_combo.currentIndexChanged.connect(self._on_remote_interface_selected)
        configuration_layout.addWidget(self._remote_interface_combo)

        credentials_header = QHBoxLayout()
        credentials_header.setSpacing(8)
        self._remote_credentials_label = QLabel(self.tr("Access credentials"))
        self._bind_theme_style(self._remote_credentials_label, self._remote_field_label_style)
        credentials_header.addWidget(self._remote_credentials_label)
        self._remote_credentials_summary = QLabel()
        self._bind_theme_style(self._remote_credentials_summary, self._remote_hint_style)
        credentials_header.addWidget(self._remote_credentials_summary, 1)
        self._remote_change_credentials_btn = QPushButton()
        self._remote_change_credentials_btn.setFixedHeight(26)
        self._remote_change_credentials_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bind_theme_style(
            self._remote_change_credentials_btn,
            settings_compact_secondary_button_stylesheet,
        )
        self._remote_change_credentials_btn.clicked.connect(self._toggle_remote_credentials_editor)
        credentials_header.addWidget(self._remote_change_credentials_btn)
        configuration_layout.addLayout(credentials_header)

        self._remote_credentials_editor = QFrame(configuration)
        self._remote_credentials_editor.setStyleSheet("background: transparent; border: none;")
        credentials_editor_layout = QVBoxLayout(self._remote_credentials_editor)
        credentials_editor_layout.setContentsMargins(0, 0, 0, 0)
        credentials_editor_layout.setSpacing(8)

        credential_row = QHBoxLayout()
        credential_row.setSpacing(8)
        self._remote_username_edit = QLineEdit(configuration)
        self._remote_username_edit.setMaxLength(64)
        self._remote_username_edit.setText(self._remote_control_credentials.configured_username())
        self._remote_username_edit.setPlaceholderText(self.tr("Username"))
        credential_row.addWidget(self._remote_username_edit, 2)
        self._remote_password_edit = QLineEdit(configuration)
        self._remote_password_edit.setMaxLength(128)
        self._remote_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._remote_password_edit.setPlaceholderText(self.tr("New password"))
        credential_row.addWidget(self._remote_password_edit, 3)
        self._remote_password_confirm_edit = QLineEdit(configuration)
        self._remote_password_confirm_edit.setMaxLength(128)
        self._remote_password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._remote_password_confirm_edit.setPlaceholderText(self.tr("Confirm password"))
        credential_row.addWidget(self._remote_password_confirm_edit, 3)
        for field in (
            self._remote_username_edit,
            self._remote_password_edit,
            self._remote_password_confirm_edit,
        ):
            field.setMinimumHeight(34)
            self._bind_theme_style(field, self._remote_field_style)
        credentials_editor_layout.addLayout(credential_row)

        credential_actions = QHBoxLayout()
        credential_actions.setSpacing(8)
        self._remote_password_hint = QLabel(
            self.tr("Use at least %1 characters. Credentials belong only to this profile.").replace(
                "%1", str(PASSWORD_MIN_LENGTH)
            )
        )
        self._remote_password_hint.setWordWrap(True)
        self._bind_theme_style(self._remote_password_hint, self._remote_hint_style)
        credential_actions.addWidget(self._remote_password_hint, 1)
        self._remote_save_credentials_btn = QPushButton(self.tr("Save credentials"))
        self._remote_save_credentials_btn.setFixedHeight(30)
        self._remote_save_credentials_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bind_theme_style(
            self._remote_save_credentials_btn,
            settings_picker_primary_button_stylesheet,
        )
        self._remote_save_credentials_btn.clicked.connect(self._save_remote_credentials)
        credential_actions.addWidget(self._remote_save_credentials_btn)
        credentials_editor_layout.addLayout(credential_actions)
        configuration_layout.addWidget(self._remote_credentials_editor)
        self._remote_credentials_editor.setHidden(
            self._remote_control_credentials.has_credentials()
        )
        self._sync_remote_credentials_editor()

        self._remote_status_label = QLabel()
        self._remote_status_label.setWordWrap(True)
        self._bind_theme_style(self._remote_status_label, self._remote_status_style)
        configuration_layout.addWidget(self._remote_status_label)

        endpoint_row = QHBoxLayout()
        endpoint_row.setSpacing(8)
        self._remote_endpoint_label = QLabel()
        self._remote_endpoint_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._bind_theme_style(self._remote_endpoint_label, self._remote_endpoint_style)
        endpoint_row.addWidget(self._remote_endpoint_label, 1)
        self._remote_copy_url_btn = QPushButton(self.tr("Copy address"))
        self._remote_copy_url_btn.setMinimumHeight(34)
        self._remote_copy_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bind_theme_style(
            self._remote_copy_url_btn,
            self._remote_copy_button_style,
        )
        self._remote_copy_feedback = ButtonConfirmationFeedback(
            self._remote_copy_url_btn,
            idle_icon=lambda: make_icon(ICON_COPY, 14, SETTINGS_TEXT_SECONDARY),
            confirmed_icon=lambda: make_icon(ICON_CHECK, 14, SETTINGS_SUCCESS),
        )
        self._add_theme_binding(self._remote_copy_feedback.apply_theme)
        self._remote_copy_url_btn.clicked.connect(self._copy_remote_url)
        endpoint_row.addWidget(self._remote_copy_url_btn)
        self._remote_setup_btn = QPushButton(self.tr("Set up a device"))
        self._remote_setup_btn.setMinimumHeight(34)
        self._remote_setup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bind_theme_style(
            self._remote_setup_btn,
            settings_picker_primary_button_stylesheet,
        )
        self._remote_setup_btn.clicked.connect(self._open_remote_setup)
        self._remote_setup_btn.setEnabled(False)
        endpoint_row.addWidget(self._remote_setup_btn)
        configuration_layout.addLayout(endpoint_row)

        layout.addWidget(configuration)
        self._refresh_remote_configuration_status()
        return card

    def _populate_remote_interfaces(self) -> None:
        selected = self._remote_control_settings.network_selection()
        selected_key = selected.selection_key if selected is not None else ""
        self._remote_interface_combo.blockSignals(True)
        self._remote_interface_combo.clear()
        self._remote_interface_combo.addItem(self.tr("Select a private network…"), "")
        self._remote_interfaces.clear()
        for interface in discover_lan_interfaces():
            self._remote_interfaces[interface.selection_key] = interface
            self._remote_interface_combo.addItem(
                f"{interface.display_name}  ·  {interface.ipv4_address}",
                interface.selection_key,
            )
        index = self._remote_interface_combo.findData(selected_key)
        self._remote_interface_combo.setCurrentIndex(max(0, index))
        self._remote_interface_combo.blockSignals(False)

    def _on_remote_interface_selected(self, index: int) -> None:
        selection_key = str(self._remote_interface_combo.itemData(index) or "")
        interface = self._remote_interfaces.get(selection_key)
        if interface is None:
            self._remote_control_settings.clear_network_selection()
        else:
            self._remote_control_settings.set_network_selection(
                interface.identifier,
                interface.ipv4_address,
            )
        self._invalidate_remote_runtime_status()
        self._refresh_remote_configuration_status()
        self.remote_control_settings_changed.emit()

    def _on_remote_control_toggled(self, enabled: bool) -> None:
        if enabled and not self._remote_configuration_is_valid():
            self._remote_control_toggle.set_checked(False)
            self._set_remote_status(
                self.tr("Choose a network and save credentials before enabling remote control."),
                "error",
            )
            return
        self._remote_setup_pending_auto_open = bool(
            enabled and not self._remote_control_settings.onboarding_seen()
        )
        self._remote_control_settings.set_enabled(enabled)
        self._invalidate_remote_runtime_status()
        self._refresh_remote_configuration_status()
        self.remote_control_settings_changed.emit()

    def _save_remote_credentials(self) -> None:
        username = self._remote_username_edit.text()
        password = self._remote_password_edit.text()
        confirmation = self._remote_password_confirm_edit.text()
        if password != confirmation:
            self._set_remote_status(self.tr("The passwords do not match."), "error")
            return
        if not password:
            self._set_remote_status(self.tr("Enter a new password to save."), "error")
            return
        try:
            self._remote_control_credentials.set_credentials(username, password)
        except (UsernameValidationError, PasswordValidationError):
            self._set_remote_status(
                self.tr(
                    "Check the username and use a password with at least %1 characters."
                ).replace("%1", str(PASSWORD_MIN_LENGTH)),
                "error",
            )
            return
        self._remote_password_edit.clear()
        self._remote_password_confirm_edit.clear()
        self._remote_username_edit.setText(self._remote_control_credentials.configured_username())
        self._remote_credentials_editor.hide()
        self._sync_remote_credentials_editor()
        self._invalidate_remote_runtime_status()
        self._refresh_remote_configuration_status()
        self.remote_control_credentials_changed.emit()

    def _toggle_remote_credentials_editor(self) -> None:
        visible = self._remote_credentials_editor.isHidden()
        self._remote_credentials_editor.setVisible(visible)
        if not visible:
            self._remote_username_edit.setText(
                self._remote_control_credentials.configured_username()
            )
            self._remote_password_edit.clear()
            self._remote_password_confirm_edit.clear()
        elif self._remote_password_edit.isVisible():
            self._remote_password_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._sync_remote_credentials_editor()

    def _sync_remote_credentials_editor(self) -> None:
        configured = self._remote_control_credentials.has_credentials()
        username = self._remote_control_credentials.configured_username()
        self._remote_credentials_summary.setText(
            self.tr("Configured as %1").replace("%1", username)
            if configured
            else self.tr("Not configured")
        )
        self._remote_change_credentials_btn.setVisible(configured)
        self._remote_change_credentials_btn.setText(
            self.tr("Cancel")
            if not self._remote_credentials_editor.isHidden()
            else self.tr("Change")
        )
        if not configured:
            self._remote_credentials_editor.show()

    def _remote_configuration_is_valid(self) -> bool:
        selection_key = str(self._remote_interface_combo.currentData() or "")
        return (
            selection_key in self._remote_interfaces
            and self._remote_control_credentials.has_credentials()
        )

    def _refresh_remote_configuration_status(self) -> None:
        selection_key = str(self._remote_interface_combo.currentData() or "")
        interface = self._remote_interfaces.get(selection_key)
        url = (
            f"https://{interface.ipv4_address}:{REMOTE_CONTROL_PORT}/remote/"
            if interface is not None
            else ""
        )
        self._remote_endpoint_label.setText(url or self.tr("No network selected"))
        self._remote_copy_url_btn.setEnabled(bool(url))
        if self._remote_control_settings.enabled() and self._remote_configuration_is_valid():
            if self._remote_runtime_status_known:
                self._set_remote_status(
                    self._remote_runtime_message,
                    self._remote_runtime_status_kind,
                )
            else:
                self._set_remote_status(self.tr("Starting secure remote control…"), "pending")
        elif self._remote_configuration_is_valid():
            self._set_remote_status(self.tr("Ready to enable."), "ready")
        else:
            self._set_remote_status(self.tr("Configuration required."), "pending")

    def _invalidate_remote_runtime_status(self) -> None:
        self._remote_runtime_status_known = False
        self._remote_runtime_message = ""
        self._remote_runtime_status_kind = "pending"

    def set_remote_control_runtime_status(
        self,
        *,
        running: bool,
        message: str,
        fingerprint: str = "",
        access_url: str = "",
        setup_url: str = "",
        verification_code: str = "",
        certificate_der: bytes = b"",
        status: str | None = None,
    ) -> None:
        resolved_status = "running" if running else (status or "error")
        self._remote_runtime_status_known = True
        self._remote_runtime_message = message
        self._remote_runtime_status_kind = resolved_status
        self._set_remote_status(message, resolved_status)
        presentation = RemoteControlSetupPresentation(
            access_url=access_url,
            setup_url=setup_url,
            verification_code=verification_code,
            fingerprint_sha256=fingerprint,
            certificate_der=certificate_der,
        )
        self._remote_setup_presentation = presentation if presentation.ready else None
        self._remote_setup_btn.setEnabled(running and presentation.ready)
        if running and presentation.ready and self._remote_setup_pending_auto_open:
            self._remote_setup_pending_auto_open = False
            self._open_remote_setup()

    def _open_remote_setup(self) -> None:
        presentation = self._remote_setup_presentation
        if presentation is None or self._remote_setup_dialog is not None:
            return
        dialog = RemoteControlSetupDialog(
            presentation,
            self._qr_generation_session_factory,
            self,
        )
        self._remote_setup_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_remote_setup_dialog", None))
        dialog.accepted.connect(self._complete_remote_setup)
        dialog.show()

    def _complete_remote_setup(self) -> None:
        self._remote_control_settings.mark_onboarding_seen()

    def _set_remote_status(self, message: str, status: str) -> None:
        self._remote_status_label.setText(message)
        self._remote_status_label.setProperty("remoteStatus", status)
        self._remote_status_label.setStyleSheet(self._remote_status_style())

    def _copy_remote_url(self) -> None:
        url = self._remote_endpoint_label.text()
        if url.startswith("https://"):
            QGuiApplication.clipboard().setText(url)
            self._remote_copy_feedback.confirm()

    def _apply_remote_control_theme(self) -> None:
        self._remote_status_label.setStyleSheet(self._remote_status_style())

    def _remote_field_label_style(self) -> str:
        return (
            f"font-size: 11px; font-weight: 600; color: {SETTINGS_TEXT_SECONDARY};"
            " background: transparent; border: none;"
        )

    def _remote_copy_button_style(self) -> str:
        return settings_compact_secondary_button_stylesheet() + (
            f'QPushButton[confirmed="true"] {{ color: {SETTINGS_SUCCESS};'
            f" border-color: {PALETTE.success_border};"
            f" background: {PALETTE.success_surface}; }}"
            f'QPushButton[confirmed="true"]:hover {{ color: {SETTINGS_SUCCESS};'
            f" border-color: {PALETTE.success_border};"
            f" background: {PALETTE.success_surface}; }}"
        )

    def _remote_field_style(self) -> str:
        return (
            f"QLineEdit, QComboBox {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 7px; padding: 0 9px; }}"
            f"QLineEdit:focus, QComboBox:focus {{ border-color: {SETTINGS_ACCENT}; }}"
            f"QComboBox QAbstractItemView {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER}; selection-background-color: {SETTINGS_BORDER}; }}"
        )

    def _remote_hint_style(self) -> str:
        return f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"

    def _remote_endpoint_style(self) -> str:
        return (
            f"font-family: monospace; font-size: 11px; color: {SETTINGS_TEXT_SECONDARY};"
            f" background: {SETTINGS_SURFACE}; border: 1px solid {SETTINGS_BORDER};"
            " border-radius: 6px; padding: 6px 8px;"
        )

    def _remote_status_style(self) -> str:
        status = self._remote_status_label.property("remoteStatus")
        color = {
            "running": SETTINGS_SUCCESS,
            "ready": SETTINGS_SUCCESS,
            "error": SETTINGS_DANGER,
            "pending": SETTINGS_MUTED,
        }.get(status, SETTINGS_MUTED)
        return (
            f"font-size: 11px; font-weight: 600; color: {color};"
            " background: transparent; border: none;"
        )

    def _retranslate_remote_control(self) -> None:
        self._remote_control_title.setText(self.tr("Remote control"))
        self._remote_control_description.setText(
            self.tr("Control Solin securely from another device on this local network.")
        )
        self._remote_network_label.setText(self.tr("Network interface"))
        self._remote_credentials_label.setText(self.tr("Access credentials"))
        self._remote_username_edit.setPlaceholderText(self.tr("Username"))
        self._remote_password_edit.setPlaceholderText(self.tr("New password"))
        self._remote_password_confirm_edit.setPlaceholderText(self.tr("Confirm password"))
        self._remote_password_hint.setText(
            self.tr("Use at least %1 characters. Credentials belong only to this profile.").replace(
                "%1", str(PASSWORD_MIN_LENGTH)
            )
        )
        self._remote_save_credentials_btn.setText(self.tr("Save credentials"))
        self._sync_remote_credentials_editor()
        self._remote_copy_url_btn.setText(self.tr("Copy address"))
        self._remote_setup_btn.setText(self.tr("Set up a device"))
        self._populate_remote_interfaces()
        self._refresh_remote_configuration_status()
