"""Qt Quick settings host; service work lives in presentation domains."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QUrl
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from solin.styles import icons
from solin.styles.theme import PALETTE
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.settings.general import GeneralSettings
from solin.ui.qml.settings.integrations import IntegrationSettings
from solin.ui.qml.settings.navigation import SettingsNavigation
from solin.ui.qml.settings.remote import RemoteSettings
from solin.ui.qml.svg_icons import SvgIconProvider

from .screen_picker_overlay import ScreenPickerOverlay


class SettingsWidget(QWidget):
    def __init__(
        self, lang_manager, screen_manager, obs_service=None, ndi_service=None, *,
        app_settings, native_scenes_enabled, obs_settings, zoom_settings,
        auto_share_settings, camera_settings, auto_key_settings, media_settings,
        playback_protection, meeting_schedule_settings, watched_folder_settings,
        yeartext_settings, background_song_settings, remote_control_settings,
        remote_control_credentials, qr_generation_session_factory,
        yeartext_service_factory, congregation_lookup_factory,
        auto_share_accessibility_trusted,
        defer_build=False, parent=None,
    ):
        super().__init__(parent)
        self._closed = False
        self.general = GeneralSettings(
            lang_manager=lang_manager, screen_manager=screen_manager,
            app_settings=app_settings, native_scenes_enabled=native_scenes_enabled,
            media_settings=media_settings, playback_protection=playback_protection,
            meeting_schedule_settings=meeting_schedule_settings,
            watched_folder_settings=watched_folder_settings,
            yeartext_settings=yeartext_settings, background_song_settings=background_song_settings,
            yeartext_service_factory=yeartext_service_factory,
            congregation_lookup_factory=congregation_lookup_factory,
            dialog_parent=self, parent=self,
        )
        self.integrations = IntegrationSettings(
            obs_service=obs_service, ndi_service=ndi_service, obs_settings=obs_settings,
            zoom_settings=zoom_settings, auto_share_settings=auto_share_settings,
            camera_settings=camera_settings, auto_key_settings=auto_key_settings,
            auto_share_accessibility_trusted=auto_share_accessibility_trusted,
            target_picker_factory=ScreenPickerOverlay, parent=self,
        )
        self.remote = RemoteSettings(
            remote_control_settings, remote_control_credentials,
            qr_generation_session_factory, dialog_parent=self, parent=self,
        )
        self.navigation = SettingsNavigation({
            "general": self.general, "integrations": self.integrations, "remote": self.remote,
        }, self)
        self._qml = QQuickWidget(self)
        self.qml_load_handle = configure_qml_host(
            self._qml, type_name="SettingsView", clear_color=PALETTE.bg0,
            context_properties={"settingsGeneral": self.general,
                                "settingsIntegrations": self.integrations,
                                "settingsRemote": self.remote,
                                "settingsNavigation": self.navigation},
            image_providers={"settingsicons": SvgIconProvider({
                "settings": icons.ICON_NAV_SETTINGS, "appearance": icons.ICON_PALETTE,
                "media": icons.ICON_FOLDER, "meetings": icons.ICON_NAV_MEETINGS,
                "projection": icons.ICON_MONITOR, "integrations": icons.ICON_PLUG,
                "automations": icons.ICON_KEYBOARD, "remote": icons.ICON_REMOTE_CONTROL,
                "about": icons.ICON_INFO_CIRCLE, "back": icons.ICON_CHEVRON_LEFT,
                "next": icons.ICON_CHEVRON_RIGHT, "down": icons.ICON_CHEVRON_DOWN,
                "up": icons.ICON_CHEVRON_UP, "close": icons.ICON_CLOSE,
                "clock": icons.ICON_CLOCK,
            }, default_icon="settings")},
            mouse_tracking=True, defer_load=defer_build,
            dismiss_text_focus_on_pointer_press=True,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._qml)

    def showEvent(self, event):
        super().showEvent(event)
        self.qml_load_handle.start()
        self.general.start_deferred_services()
        self.general.refresh_runtime()
        self.integrations.refresh_runtime()
        self.remote.refresh_runtime()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange and hasattr(self, "_qml"):
            self.retranslateUi()

    def retranslateUi(self):  # noqa: N802 - application language host contract
        if self._closed:
            return
        for domain in (self.general, self.integrations, self.remote):
            domain.refresh_language()
        self.navigation.refresh_language()
        self._qml.engine().retranslate()

    def apply_theme(self):
        if not self._closed:
            apply_qml_theme(self._qml)
            self.general.refresh_language()

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        self.qml_load_handle.cancel()
        self._qml.setSource(QUrl())
        for domain in (self.general, self.integrations, self.remote):
            domain.cleanup()
