from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
)
from PySide6.QtCore import (
    Qt,
    Signal,
    QEvent,
    QObject,
    QTimer,
)

from ..core.i18n.manager import LanguageManager
from ..ui.screens import ScreenManager
from ..core.integrations.automation.obs import OBSWebSocketService
from ..core.integrations.automation.settings import (
    AutoKeySettingsStore,
    AutoShareSettingsStore,
    CameraSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from ..core.integrations.ndi import NDIReceiverService
from ..core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from ..core.jw.background_song_settings import BackgroundSongSettingsStore
from ..core.jw.yeartext_settings import YeartextSettingsStore
from ..core.media.settings import MediaSettingsStore
from ..core.meetings.schedule_settings import MeetingScheduleSettingsStore
from ..core.remote_control.security import RemoteControlCredentialsStore
from ..core.remote_control.settings import RemoteControlSettingsStore
from ..core.foundation.settings_store import ProfileAppSettingsStore
from ..styles.theme import available_themes, current_theme, normalize_theme_id
from ..ui.controls import NoScrollComboBox
from ..ui.loading_placeholder import DeferredLoadingPlaceholder
from .settings.about_section import AboutSectionMixin
from .settings.auto_keys_section import AutoKeysSectionMixin
from .settings.auto_share_section import AutoShareSectionMixin
from .settings.camera_section import CameraSectionMixin
from .settings.language_section import LanguageSectionMixin
from .settings.layout_helpers import SettingsLayoutMixin
from .settings.media_section import MediaSectionMixin
from .settings.meeting_schedule_section import MeetingScheduleSectionMixin
from .settings.obs_section import ObsSectionMixin
from .settings.remote_control_section import RemoteControlSectionMixin
from .settings.screens_section import ScreensSectionMixin
from .settings.shared import SettingsToggleSwitch
from .settings.watched_folder_section import WatchedFolderSectionMixin
from .settings.yearly_text_section import YearlyTextSectionMixin
from .settings.zoom_section import ZoomSectionMixin
import sys

if TYPE_CHECKING:
    from ..core.jw.yeartext import YeartextService
    from ..ui.qr_generation import QrGenerationSessionFactory


# ═══════════════════════════════════════════════════════════════════════════
# SettingsWidget
# ═══════════════════════════════════════════════════════════════════════════
class SettingsWidget(
    AboutSectionMixin,
    YearlyTextSectionMixin,
    LanguageSectionMixin,
    MeetingScheduleSectionMixin,
    MediaSectionMixin,
    RemoteControlSectionMixin,
    ObsSectionMixin,
    CameraSectionMixin,
    ZoomSectionMixin,
    AutoShareSectionMixin,
    ScreensSectionMixin,
    WatchedFolderSectionMixin,
    AutoKeysSectionMixin,
    SettingsLayoutMixin,
    QWidget,
):
    language_changed = Signal(str)
    yearly_text_changed = Signal(str, str, str)
    watched_folder_changed = Signal(str)
    zoom_enabled_toggled = Signal(bool)
    zoom_participants_toggled = Signal(bool)
    obs_stream_config_changed = Signal()
    camera_enabled_toggled = Signal(bool)
    vcam_autostart_toggled = Signal(bool)
    background_song_toggled = Signal(bool)
    meetings_auto_download_toggled = Signal(bool)
    meeting_schedule_changed = Signal()
    theme_changed = Signal(str)
    remote_control_settings_changed = Signal()
    remote_control_credentials_changed = Signal()

    def __init__(
        self,
        lang_manager: LanguageManager,
        screen_manager: ScreenManager,
        obs_service: OBSWebSocketService | None = None,
        ndi_service: NDIReceiverService | None = None,
        *,
        app_settings: ProfileAppSettingsStore,
        obs_settings: OBSSettingsStore,
        zoom_settings: ZoomSettingsStore,
        auto_share_settings: AutoShareSettingsStore,
        camera_settings: CameraSettingsStore,
        auto_key_settings: AutoKeySettingsStore,
        media_settings: MediaSettingsStore,
        playback_protection,
        meeting_schedule_settings: MeetingScheduleSettingsStore,
        watched_folder_settings: WatchedFolderSettingsStore,
        yeartext_settings: YeartextSettingsStore,
        background_song_settings: BackgroundSongSettingsStore,
        remote_control_settings: RemoteControlSettingsStore,
        remote_control_credentials: RemoteControlCredentialsStore,
        qr_generation_session_factory: QrGenerationSessionFactory,
        yeartext_service_factory: Callable[[QObject], YeartextService],
        auto_share_accessibility_trusted: Callable[[], bool],
        defer_build: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.lang = lang_manager
        self.screen_mgr = screen_manager
        self._obs = obs_service
        self._ndi = ndi_service
        self._app_settings = app_settings
        self._yeartext_service_factory = yeartext_service_factory
        self._obs_settings = obs_settings
        self._zoom_settings = zoom_settings
        self._auto_share_settings = auto_share_settings
        self._camera_settings = camera_settings
        self._auto_key_settings = auto_key_settings
        self._media_settings = media_settings
        self._playback_protection = playback_protection
        self._meeting_schedule_settings = meeting_schedule_settings
        self._watched_folder_settings = watched_folder_settings
        self._yeartext_settings = yeartext_settings
        self._background_song_settings = background_song_settings
        self._remote_control_settings = remote_control_settings
        self._remote_control_credentials = remote_control_credentials
        self._qr_generation_session_factory = qr_generation_session_factory
        self._remote_setup_pending_auto_open = False
        self._remote_setup_presentation = None
        self._remote_setup_dialog = None
        self._remote_runtime_status_known = False
        self._remote_runtime_message = ""
        self._remote_runtime_status_kind = "pending"
        self._auto_share_accessibility_trusted = auto_share_accessibility_trusted
        self._theme_persistent_connections: set[str] = set()
        self._deferred_services_started = False
        self._ui_ready = False
        self._pending_meeting_schedule_focus = False
        self._init_yearly_text_section()
        self._mark_startup("settings_service_initialized")
        if defer_build:
            self._prepare_incremental_ui()
        else:
            self.preparation_handle = None
            self._build_ui()
            self._ui_ready = True
        screen_manager.screens_changed.connect(self._refresh_screens)
        lang_manager.language_changed.connect(self._on_language_switched)
        lang_manager.jw_lang_service.media_language_changed.connect(self._on_language_switched)

    def start_deferred_services(self) -> None:
        """Start non-critical settings services once the first frame is visible."""
        if self._deferred_services_started:
            return
        self._deferred_services_started = True
        self.lang.jw_lang_service.fetch_if_needed()
        self._check_and_fetch_yeartext()

    def showEvent(self, event):
        super().showEvent(event)
        if self.preparation_handle is not None:
            self.preparation_handle.start()
        self.start_deferred_services()
        if not getattr(self, "_ui_ready", True):
            return
        self._refresh_autoshare_accessibility_status()
        self._populate_remote_interfaces()
        self._refresh_remote_configuration_status()

    # ── build UI ───────────────────────────────────────────────────────────

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            child_widget = item.widget()
            if child_layout is not None:
                self._clear_layout(child_layout)
            if child_widget is not None:
                child_widget.hide()
                child_widget.deleteLater()

    def _build_theme_selector(self, header: QHBoxLayout) -> None:
        self._theme_combo = NoScrollComboBox(self)
        self._theme_combo.setFixedHeight(34)
        self._theme_combo.setMinimumWidth(120)
        header.addWidget(self._theme_combo)
        self._populate_theme_selector()
        self._theme_combo.currentIndexChanged.connect(self._on_theme_selected)

    def _build_ui(self) -> None:
        self._begin_ui_build(self)
        for unit in self._settings_build_units():
            unit()

    def _prepare_incremental_ui(self) -> None:
        from ..ui.incremental_load import IncrementalLoadHandle

        self._mark_startup("settings_placeholder_constructed")
        self._loading_placeholder = DeferredLoadingPlaceholder(
            self.tr("Loading…"),
            self,
        )
        self._mark_startup("settings_stack_constructed")
        self._apply_loading_placeholder_theme()
        self._mark_startup("settings_placeholder_styled")
        self.preparation_handle = IncrementalLoadHandle(
            (self._begin_incremental_ui_build, *self._settings_build_units()),
            self,
        )
        self._mark_startup("settings_preparation_created")

    def _begin_incremental_ui_build(self) -> None:
        self._settings_page = QWidget(self)
        self._settings_page.hide()
        self._begin_ui_build(self._settings_page)
        self._mark_startup("settings_shell_constructed")

    def _begin_ui_build(self, page: QWidget) -> None:
        self._reset_theme_bindings()
        outer = page.layout()
        if outer is None:
            outer = QVBoxLayout(page)
        else:
            self._clear_layout(outer)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        self._main_title = QLabel(self.tr("Settings"))
        self._main_title.setObjectName("SectionTitle")
        header.addWidget(self._main_title)
        header.addStretch()
        self._build_theme_selector(header)
        outer.addLayout(header)
        outer.addSpacing(20)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setSpacing(0)
        sections.setContentsMargins(0, 0, 4, 0)
        self._settings_outer_layout = outer
        self._settings_scroll = scroll
        self._settings_content = content
        self._settings_sections_layout = sections
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _settings_build_units(self) -> tuple[Callable[[], None], ...]:
        return (
            self._build_language_settings_unit,
            self._build_media_settings_unit,
            self._build_meeting_settings_unit,
            self._build_folder_settings_unit,
            self._build_remote_settings_unit,
            self._build_obs_settings_unit,
            self._build_camera_settings_unit,
            self._build_zoom_settings_unit,
            self._build_auto_share_settings_unit,
            self._build_auto_keys_settings_unit,
            self._build_yeartext_settings_unit,
            self._build_screens_settings_unit,
            self._build_about_settings_unit,
            self._finish_ui_build,
        )

    def _add_settings_section(self, title: str, attr: str, card: QWidget) -> None:
        layout = self._settings_sections_layout
        layout.addWidget(self._section_title(self.tr(title), attr))
        layout.addSpacing(8)
        layout.addWidget(card)
        layout.addSpacing(20)

    def _build_language_settings_unit(self) -> None:
        self._add_settings_section("Language", "_lang_section_title", self._build_lang_card())
        self._mark_startup("settings_language_constructed")

    def _build_media_settings_unit(self) -> None:
        self._add_settings_section("Media", "_media_section_title", self._build_media_card())
        self._mark_startup("settings_media_constructed")

    def _build_meeting_settings_unit(self) -> None:
        self._add_settings_section(
            "Meetings",
            "_meetings_section_title",
            self._build_meeting_schedule_card(),
        )
        self._mark_startup("settings_meetings_constructed")

    def _build_folder_settings_unit(self) -> None:
        self._add_settings_section(
            "Folders",
            "_folders_section_title",
            self._build_watched_folder_card(),
        )
        self._mark_startup("settings_folders_constructed")

    def _build_remote_settings_unit(self) -> None:
        self._add_settings_section(
            "Remote access",
            "_remote_control_section_title",
            self._build_remote_control_card(),
        )
        self._mark_startup("settings_remote_constructed")

    def _build_obs_settings_unit(self) -> None:
        layout = self._settings_sections_layout
        layout.addWidget(
            self._section_title(self.tr("Integrations"), "_integrations_section_title")
        )
        layout.addSpacing(8)
        layout.addWidget(self._build_obs_card())

    def _build_camera_settings_unit(self) -> None:
        self._settings_sections_layout.addSpacing(16)
        self._settings_sections_layout.addWidget(self._build_camera_card())

    def _build_zoom_settings_unit(self) -> None:
        if sys.platform != "win32":
            return
        self._settings_sections_layout.addSpacing(16)
        self._settings_sections_layout.addWidget(self._build_zoom_card())

    def _build_auto_share_settings_unit(self) -> None:
        self._settings_sections_layout.addSpacing(16)
        self._settings_sections_layout.addWidget(self._build_auto_share_card())

    def _build_auto_keys_settings_unit(self) -> None:
        self._settings_sections_layout.addSpacing(16)
        self._settings_sections_layout.addWidget(self._build_auto_keys_card())
        self._settings_sections_layout.addSpacing(20)
        self._mark_startup("settings_integrations_constructed")

    def _build_yeartext_settings_unit(self) -> None:
        self._add_settings_section(
            "Annual Text",
            "_yearly_section_title",
            self._build_yearly_text_card(),
        )
        self._mark_startup("settings_yeartext_constructed")

    def _build_screens_settings_unit(self) -> None:
        self._screens_card, self._screens_card_lay = self._card()
        self._populate_screens()
        self._add_settings_section("Screens", "_screens_section_title", self._screens_card)
        self._mark_startup("settings_screens_constructed")

    def _build_about_settings_unit(self) -> None:
        layout = self._settings_sections_layout
        layout.addWidget(self._section_title(self.tr("About"), "_about_section_title"))
        layout.addSpacing(8)
        layout.addWidget(self._build_about_card())
        layout.addStretch()
        self._mark_startup("settings_about_constructed")

    def _finish_ui_build(self) -> None:
        self._ui_ready = True
        self._loading_placeholder.finish()
        self._sync_yeartext_ui()
        settings_page = getattr(self, "_settings_page", None)
        if settings_page is not None and settings_page is not self:
            settings_page.setGeometry(self.rect())
            settings_page.show()
        self._refresh_remote_configuration_status()
        if self._pending_meeting_schedule_focus:
            self._pending_meeting_schedule_focus = False
            self.focus_meeting_schedule()

    def _apply_loading_placeholder_theme(self) -> None:
        placeholder = getattr(self, "_loading_placeholder", None)
        if placeholder is not None:
            placeholder.refresh_theme()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        settings_page = getattr(self, "_settings_page", None)
        if settings_page is not None and settings_page is not self:
            settings_page.setGeometry(self.rect())

    @staticmethod
    def _mark_startup(name: str) -> None:
        from ..bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark(name)

    def apply_theme(self) -> None:
        if not getattr(self, "_ui_ready", True):
            self._apply_loading_placeholder_theme()
            return
        self._apply_settings_theme_bindings()
        for section_refresh in (
            "_apply_yearly_text_theme",
            "_apply_obs_theme",
            "_apply_auto_share_theme",
            "_apply_auto_keys_theme",
            "_apply_watched_folder_theme",
            "_apply_remote_control_theme",
            "_apply_about_theme",
            "_apply_zoom_theme",
            "_apply_meeting_schedule_theme",
            "_apply_screens_theme",
        ):
            refresh = getattr(self, section_refresh, None)
            if refresh is not None:
                refresh()
        for toggle in self.findChildren(SettingsToggleSwitch):
            toggle.update()
        self.update()

    def focus_meeting_schedule(self) -> None:
        """Reveal the canonical meeting schedule card after contextual navigation."""

        if not self._ui_ready:
            self._pending_meeting_schedule_focus = True
            if self.preparation_handle is not None:
                self.preparation_handle.start()
            return

        card = getattr(self, "_meeting_schedule_card", None)
        if card is None:
            return

        def reveal() -> None:
            self._settings_scroll.ensureWidgetVisible(card, 0, 28)
            rows = getattr(self, "_schedule_rows", {})
            first_controls = next(iter(rows.values()), ())
            focus_target = first_controls[0] if first_controls else card
            focus_target.setFocus(Qt.FocusReason.OtherFocusReason)

        QTimer.singleShot(0, reveal)

    # ── i18n ───────────────────────────────────────────────────────────────

    def _populate_theme_selector(self) -> None:
        themes = available_themes()
        current_id = normalize_theme_id(self._app_settings.app_theme_id())
        self._theme_combo.blockSignals(True)
        self._theme_combo.clear()
        for theme in themes:
            self._theme_combo.addItem(self._theme_display_name(theme), theme.id)
        selected_index = self._theme_combo.findData(current_id)
        if selected_index >= 0:
            self._theme_combo.setCurrentIndex(selected_index)
        self._theme_combo.setVisible(len(themes) > 1)
        self._theme_combo.blockSignals(False)

    def _theme_display_name(self, theme) -> str:
        if theme.id == "dark":
            return self.tr("Dark")
        if theme.id == "light":
            return self.tr("Light")
        return self.tr(theme.display_name)

    def _on_theme_selected(self, index: int) -> None:
        theme_id = self._theme_combo.itemData(index)
        if theme_id is None:
            return
        normalized = normalize_theme_id(str(theme_id))
        previous_setting = normalize_theme_id(self._app_settings.app_theme_id())
        if normalized == previous_setting and normalized == current_theme().id:
            return
        self._app_settings.set_app_theme_id(normalized)
        signal = getattr(self, "theme_changed", None)
        if signal is not None and normalized != current_theme().id:
            signal.emit(normalized)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            if not self._ui_ready:
                self._loading_placeholder.set_text(self.tr("Loading…"))
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self):
        if not self._ui_ready:
            return
        self._main_title.setText(self.tr("Settings"))
        self._populate_theme_selector()
        self._lang_section_title.setText(self.tr("Language").upper())
        self._media_section_title.setText(self.tr("Media").upper())
        self._meetings_section_title.setText(self.tr("Meetings").upper())
        self._folders_section_title.setText(self.tr("Folders").upper())
        self._remote_control_section_title.setText(self.tr("Remote access").upper())
        self._integrations_section_title.setText(self.tr("Integrations").upper())
        self._yearly_section_title.setText(self.tr("Annual Text").upper())
        self._screens_section_title.setText(self.tr("Screens").upper())
        self._about_section_title.setText(self.tr("About").upper())
        self._ui_lang_label.setText(self.tr("Interface"))
        self._media_lang_label.setText(self.tr("JW Media"))
        self._refresh_lang_row()
        self._refresh_media_lang_row()
        self._auto_dl_label.setText(self.tr("Auto-download on play"))
        self._auto_dl_desc.setText(self.tr("Downloads the playing media for offline use."))
        self._meetings_dl_label.setText(self.tr("Auto-download weekly study"))
        self._meetings_dl_desc.setText(
            self.tr("Downloads this week\u2019s and next week\u2019s meeting media.")
        )
        self._sjjm_announce_label.setText(self.tr("Song Announcement Mode"))
        self._sjjm_announce_desc.setText(
            self.tr("Song starts muted for title display. Press play to start.")
        )
        self._background_song_label.setText(self.tr("Automatic background song"))
        self._sync_background_song_desc()
        self._start_paused_label.setText(self.tr("Start videos paused"))
        self._start_paused_desc.setText(
            self.tr("Videos open paused so you can start them manually.")
        )
        self._playback_protection_label.setText(self.tr("Playback protection"))
        self._playback_protection_desc.setText(
            self.tr(
                "Prevents media changes and seeking while audio or video is playing. "
                "Pause first to make changes."
            )
        )
        self._watched_folder_title_lbl.setText(self.tr("Link Folder"))
        self._watched_folder_desc_lbl.setText(
            self.tr("Sync folder (Dropbox, OneDrive, etc.) shown as playlists.")
        )
        self._sync_watched_folder_path_label()
        self._retranslate_remote_control()
        self._watched_folder_pick_btn.setText(self.tr("Choose\u2026"))
        self._obs_header_lbl.setText(self.tr("OBS Studio"))
        self._obs_header_desc.setText(self.tr("Automatically switches scenes during projection"))
        self._obs_port_lbl.setText(self.tr("WebSocket Port"))
        self._obs_pwd_lbl.setText(self.tr("Password (optional)"))
        self._obs_pwd_edit.setPlaceholderText(self.tr("Leave blank if no password is set"))
        self._obs_default_lbl.setText(self.tr("Default scene (idle)"))
        self._obs_default_hint.setText(self.tr("Scene shown when nothing is being projected."))
        self._obs_media_lbl.setText(self.tr("Media window scene"))
        self._obs_media_hint.setText(
            self.tr(
                "Scene that captures the projection monitor. Activated when content is displayed."
            )
        )
        self._obs_stream_title_lbl.setText(self.tr("Program stream (NDI)"))
        self._obs_stream_desc_lbl.setText(
            self.tr("Receive the DistroAV/NDI output from OBS as a live projection.")
        )
        self._obs_stream_hint_lbl.setText(
            self.tr("Enable Main Output in DistroAV, then select the NDI source shown by OBS.")
        )
        self._obs_stream_source_lbl.setText(self.tr("Available NDI sources"))
        self._obs_stream_refresh_btn.setText(self.tr("Find sources"))
        if self._obs:
            self._sync_obs_ui_state(self._obs.state, "")
        self._camera_label.setText(self.tr("Camera"))
        self._camera_desc.setText(self.tr("Shows a camera button in the live tools toolbar."))
        self._auto_keys_header_lbl.setText(self.tr("Automatic Shortcuts"))
        self._auto_keys_header_desc.setText(
            self.tr("Sends keyboard shortcuts when visual media changes state.")
        )
        self._auto_keys_hint_lbl.setText(
            self.tr("Create one or more shortcuts for start, end, pause and resume events.")
        )
        self._auto_keys_empty_lbl.setText(self.tr("No shortcuts configured."))
        self._auto_keys_add_btn.setText(self.tr("Add shortcut"))
        self._refresh_auto_keys_list()
        self._refresh_meeting_schedule_texts()

        if sys.platform == "win32":
            if hasattr(self, "_zoom_enabled_label"):
                self._zoom_enabled_label.setText(self.tr("Zoom Meetings"))
                self._zoom_enabled_desc.setText(
                    self.tr("Audio controls and attendance count during meetings.")
                )
        if hasattr(self, "_autoshare_label"):
            self._autoshare_label.setText(self.tr("Auto Screen Share"))
            self._autoshare_desc.setText(
                self.tr("Automatically shares screen via hotkeys when projecting media.")
            )
            self._autoshare_hotkey_lbl.setText(self.tr("Share hotkey"))
            self._autoshare_hotkey_hint.setText(
                self.tr("Uses Zoom's single start/stop screen-share shortcut.")
            )
            self._autoshare_hotkey_btn.setToolTip(self.tr("Edit"))
            self._autoshare_pos_title.setText(self.tr("Share target"))
            self._autoshare_pos_desc.setText(
                self.tr("The Solin Media Preview tile inside Zoom's share dialog.")
            )
            self._autoshare_config_btn.setText(self.tr("Configure"))
            if sys.platform == "darwin" and hasattr(self, "_autoshare_access_title"):
                self._autoshare_access_title.setText(self.tr("Accessibility permission"))
                self._autoshare_access_btn.setText(self.tr("Open Settings"))
            self._refresh_autoshare_hotkey_label()
            self._refresh_autoshare_position_label()
            self._refresh_autoshare_accessibility_status()
        self._yearly_hint_lbl.setText(self.tr("Text shown on the projection screen when idle."))
        self._yt_refresh_btn.setText(self.tr("Update"))
        self._update_manual_toggle_label()
        self._yearly_quote_lbl.setText(self.tr("Scripture:"))
        self._yearly_quote_edit.setPlaceholderText(
            self.tr("E.g.: Happy are those conscious of their spiritual need.")
        )
        self._yearly_ref_lbl.setText(self.tr("Bible reference:"))
        self._yearly_ref_edit.setPlaceholderText(self.tr("E.g.: Matthew 5:3."))
        self._yearly_save_btn.setText(self.tr("Save changes"))
        self._about_desc_lbl.setText(self.tr("Audio & Video app for Kingdom Hall meetings."))
        self._disclaimer_lbl.setText(
            self.tr(
                "This app is independent and is not affiliated with or endorsed by "
                "the Watch Tower Bible and Tract Society of Pennsylvania or any of "
                "its associated organizations."
            )
        )
        self._link_site_btn.setText(self.tr("Official Website"))
        self._link_changelog_btn.setText(self.tr("Changelog"))
        self._refresh_screens()
