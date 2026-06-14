from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.meetings.tree_store import flush_meeting_thumbs_dir
from ..styles.icons import (
    ICON_NAV_BROWSER,
    ICON_NAV_CACHE,
    ICON_NAV_CLIPS,
    ICON_NAV_MEETINGS,
    ICON_NAV_PLAYLIST,
    ICON_NAV_SETTINGS,
    ICON_NAV_SONGS,
    ICON_NAV_THEME,
    ICON_NAV_TIMER,
    ICON_NAV_WIFI,
)
from ..widgets.clips_widget import ClipsWidget
from ..widgets.common.profile_avatar_button import ProfileAvatarButton
from ..widgets.common.sidebar_button import SidebarButton
from ..widgets.meetings.widget import MeetingsWidget
from ..core.playlists.cleanup import (
    flush_embedded_dir,
    flush_images_dir,
    flush_pdf_pages,
    flush_pending_deletions,
    flush_pptx_pages,
    flush_thumbs_dir,
)
from ..widgets.playlist.widget import PlaylistWidget
from ..widgets.projection.bar import ProjectionBar
from ..widgets.quick_access_toolbar import QuickAccessToolbar
from ..widgets.sermon_theme_widget import SermonThemeWidget
from ..widgets.settings_widget import SettingsWidget
from ..widgets.songs_widget import SongsWidget
from ..widgets.timer_widget import TimerWidget
from .lazy_page_controller import LazyPageController
from .main_window_nav import (
    NAV_LABELS,
    SIDEBAR_SUBTITLE_SOURCE,
    SIDEBAR_TITLE_SOURCE,
    SWITCH_PROFILE_SOURCE,
)
from .navigation_controller import NavigationController


class MainWindowUiController:
    """Builds the MainWindow widget tree and static navigation chrome."""

    _NAV_BUTTON_ICONS = (
        ICON_NAV_SONGS,
        ICON_NAV_MEETINGS,
        ICON_NAV_BROWSER,
        ICON_NAV_CLIPS,
        ICON_NAV_TIMER,
        ICON_NAV_THEME,
        ICON_NAV_SETTINGS,
        ICON_NAV_PLAYLIST,
        ICON_NAV_CACHE,
        ICON_NAV_WIFI,
    )
    _NAV_BUTTON_SPECS = tuple(
        (attr_name, icon, label, index)
        for index, ((attr_name, label), icon) in enumerate(
            zip(NAV_LABELS, _NAV_BUTTON_ICONS, strict=True)
        )
    )
    _SIDEBAR_LAYOUT_ORDER = (
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_timer_btn",
        "nav_playlist_btn",
        "nav_songs_btn",
        "nav_clips_btn",
        "nav_theme_btn",
        "nav_cache_btn",
        "nav_wifi_btn",
    )

    def __init__(self, window, active_profile) -> None:
        self._window = window
        self._active_profile = active_profile

    @classmethod
    def nav_button_specs(cls) -> tuple[tuple[str, str, str, int], ...]:
        return cls._NAV_BUTTON_SPECS

    @classmethod
    def sidebar_layout_order(cls) -> tuple[str, ...]:
        return cls._SIDEBAR_LAYOUT_ORDER

    def build_ui(self) -> None:
        window = self._window
        central = QWidget()
        window.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        window.stack = QStackedWidget()
        window.stack.setObjectName("ContentArea")
        window._lazy_pages = LazyPageController(window)
        window._navigation = NavigationController(window)

        self._build_pages()

        root.addWidget(self.build_sidebar())

        window.right_col = QWidget()
        window.right_col.setContentsMargins(0, 0, 0, 0)
        right_layout = QVBoxLayout(window.right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(window.stack, stretch=1)
        right_layout.addWidget(self.build_bottom_bar())

        root.addWidget(window.right_col, stretch=1)

        self._build_quick_toolbar()
        self._prime_native_cursor_hosts()
        window.right_col.installEventFilter(window)

    def build_sidebar(self) -> QFrame:
        window = self._window
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        window._sidebar_title_lbl = QLabel(window.tr(SIDEBAR_TITLE_SOURCE))
        window._sidebar_title_lbl.setObjectName("SectionTitle")
        window._sidebar_title_lbl.setStyleSheet(
            "background: transparent; font-size: 14px; font-weight: 700; "
            "padding: 4px 8px 2px 8px;"
        )
        window._sidebar_subtitle_lbl = QLabel(window.tr(SIDEBAR_SUBTITLE_SOURCE))
        window._sidebar_subtitle_lbl.setObjectName("SectionSubtitle")
        window._sidebar_subtitle_lbl.setStyleSheet(
            "background: transparent; padding: 0 8px 12px 8px;"
        )

        layout.addLayout(self._build_sidebar_header())
        layout.addWidget(self._separator())
        layout.addSpacing(4)

        self._build_nav_buttons()
        for attr_name in self._SIDEBAR_LAYOUT_ORDER:
            layout.addWidget(getattr(window, attr_name))
        layout.addStretch()
        layout.addWidget(window.nav_settings_btn)

        window._nav_btns = [
            getattr(window, attr_name)
            for attr_name, _icon, _label, _index in self._NAV_BUTTON_SPECS
        ]
        window._navigation.switch_page(1)
        return sidebar

    def build_bottom_bar(self) -> QFrame:
        window = self._window
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        window.proj_bar = ProjectionBar(
            window.media_ctrl,
            playback_settings=window._projection_playback_settings,
            profile_paths=window.profile_paths,
            media_cache_dir=window.media_cache_manager.media_cache_dir,
            thumb_cache_dir=window.runtime_paths.thumb_cache_dir,
            lang_manager=window.lang,
            container=window.right_col,
        )
        window.proj_bar.setFixedHeight(48)
        window.proj_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        window.proj_bar.setStyleSheet(
            "QFrame#StatusBar { border: none; background: transparent; }"
        )
        layout.addWidget(window.proj_bar)
        return bar

    def _build_pages(self) -> None:
        window = self._window
        window.songs_widget = SongsWidget(
            window.lang,
            window.media_cache_manager,
            window.jw_songs_store,
            window.runtime_paths.cache_dir,
            window.media_ctrl,
            parent=window,
        )
        window.settings_widget = SettingsWidget(
            window.lang,
            window.screen_mgr,
            obs_service=window._obs_service,
            ndi_service=window._ndi_service,
            obs_settings=window._obs_settings,
            zoom_settings=window._zoom_settings,
            auto_share_settings=window._auto_share_settings,
            camera_settings=window._camera_settings,
            auto_key_settings=window._auto_key_settings,
            media_settings=window._media_settings,
            meeting_schedule_settings=window._meeting_schedule_settings,
            watched_folder_settings=window._watched_folder_settings,
            yeartext_settings=window._yeartext_settings,
            background_song_settings=window._background_song_settings,
            yeartext_cache_file=(
                window.runtime_paths.cache_dir / "yeartext_cache.json"
            ),
            parent=window,
        )
        window.timer_widget = TimerWidget(
            window.lang,
            bridge=window.timer_bridge,
            parent=window,
        )
        window.clips_widget = ClipsWidget(
            window.lang,
            window.media_cache_manager,
            window.runtime_paths.cache_dir,
            window.media_ctrl,
            parent=window,
        )
        window.sermon_theme_widget = SermonThemeWidget(window.lang, parent=window)
        watched_folder = window.settings_widget.get_watched_folder()
        window.playlist_widget = PlaylistWidget(
            window.lang,
            media_ctrl=window.media_ctrl,
            watched_folder=watched_folder,
            notifications=window.notifications,
            profile_paths=window.profile_paths,
            runtime_paths=window.runtime_paths,
            storage_paths=window.playlist_storage_paths,
            media_cache_manager=window.media_cache_manager,
            jw_catalog_cache_paths=window.jw_catalog_cache_paths,
            jw_songs_store=window.jw_songs_store,
            thumb_cache_dir=window.runtime_paths.thumb_cache_dir,
            parent=window,
        )
        window.meetings_widget = MeetingsWidget(
            window.lang,
            meeting_tree_store=window.meeting_tree_store,
            profile_paths=window.profile_paths,
            runtime_paths=window.runtime_paths,
            cache_manager=window.media_cache_manager,
            jw_catalog_cache_paths=window.jw_catalog_cache_paths,
            jw_songs_store=window.jw_songs_store,
            jwpub_checksum_store=window.jwpub_checksum_store,
            media_settings=window._media_settings,
            meeting_schedule_settings=window._meeting_schedule_settings,
            parent=window,
        )
        window.meetings_widget.set_watched_folder(watched_folder)

        self._flush_orphaned_media_files()

        window.stack.addWidget(window.songs_widget)
        window.stack.addWidget(window.meetings_widget)
        window.stack.addWidget(window._lazy_pages.placeholder())
        window.stack.addWidget(window.clips_widget)
        window.stack.addWidget(window.timer_widget)
        window.stack.addWidget(window.sermon_theme_widget)
        window.stack.addWidget(window.settings_widget)
        window.stack.addWidget(window.playlist_widget)
        window.stack.addWidget(window._lazy_pages.placeholder())
        window.stack.addWidget(window._lazy_pages.placeholder())

    def _build_quick_toolbar(self) -> None:
        window = self._window
        window._quick_toolbar = QuickAccessToolbar(
            window._obs_service,
            window._zoom_service,
            window._camera_service,
            window.right_col,
            obs_settings=window._obs_settings,
            camera_settings=window._camera_settings,
            background_song_service=window._background_song_service,
        )
        window._quick_toolbar.monitor_clicked.connect(
            window._projection_targets.on_monitor_manager_requested
        )
        window._quick_toolbar.obs_scene_change.connect(
            window._live_integrations.on_quick_obs_scene_change
        )
        window._quick_toolbar.obs_return_scene_change.connect(
            window._live_integrations.on_quick_obs_return_scene_change
        )
        window._quick_toolbar.obs_stream_requested.connect(
            window._live_integrations.project_obs_ndi_stream
        )
        window._quick_toolbar.obs_camera_stream_requested.connect(
            window._live_integrations.project_camera_stream
        )
        window._quick_toolbar.camera_stream_requested.connect(
            window._live_integrations.project_camera_stream
        )
        window._quick_toolbar.camera_selection_changed.connect(
            window._live_integrations.on_camera_selection_changed
        )
        window._quick_toolbar.set_camera_enabled(
            window.settings_widget.get_camera_enabled()
        )
        window._quick_toolbar.show()
        window._quick_toolbar.reposition()
        window._navigation.update_quick_toolbar_browser_style()

    def _prime_native_cursor_hosts(self) -> None:
        window = self._window
        for widget in (window.right_col, window.stack):
            widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            widget.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
            widget.winId()

    def _build_sidebar_header(self) -> QHBoxLayout:
        window = self._window
        profile_name = self._active_profile.name

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_col.addWidget(window._sidebar_title_lbl)
        title_col.addWidget(window._sidebar_subtitle_lbl)

        window._profile_avatar_btn = ProfileAvatarButton(profile_name)
        window._profile_avatar_btn.setToolTip(window.tr(SWITCH_PROFILE_SOURCE))
        window._profile_avatar_btn.clicked.connect(window._profile_switch.request_switch)

        header_row.addLayout(title_col, 1)
        header_row.addWidget(
            window._profile_avatar_btn,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        return header_row

    def _build_nav_buttons(self) -> None:
        window = self._window
        for attr_name, icon, label, page_index in self._NAV_BUTTON_SPECS:
            button = SidebarButton(icon, window.tr(label))
            button.clicked.connect(
                lambda _checked=False, index=page_index: window._navigation.switch_page(
                    index
                )
            )
            setattr(window, attr_name, button)

    @staticmethod
    def _separator() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#21262d; border:none; margin: 4px 0;")
        return sep

    def _flush_orphaned_media_files(self) -> None:
        storage_paths = self._window.playlist_storage_paths
        runtime_paths = self._window.runtime_paths
        meeting_tree_store = self._window.meeting_tree_store
        flush_pending_deletions(storage_paths, meeting_tree_store)
        profile_paths = self._window.profile_paths
        flush_images_dir(storage_paths, meeting_tree_store, profile_paths)
        flush_thumbs_dir(storage_paths, runtime_paths.thumb_cache_dir)
        flush_meeting_thumbs_dir(
            store=meeting_tree_store,
            thumb_dir=runtime_paths.meeting_thumb_cache_dir,
        )
        flush_embedded_dir(storage_paths, meeting_tree_store, profile_paths)
        flush_pdf_pages(storage_paths, meeting_tree_store, runtime_paths.pdf_pages_dir)
        flush_pptx_pages(
            storage_paths,
            meeting_tree_store,
            runtime_paths.pptx_pages_dir,
            runtime_paths.docx_pages_dir,
        )
