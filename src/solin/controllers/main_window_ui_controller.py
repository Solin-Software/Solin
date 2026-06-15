from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, Qt
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
from ..core.playlists.cleanup import (
    flush_embedded_dir,
    flush_images_dir,
    flush_pdf_pages,
    flush_pending_deletions,
    flush_pptx_pages,
    flush_thumbs_dir,
)
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
from ..ui.media_info import MediaInfoQueue, MediaInfoService
from ..widgets.meetings.widget import MeetingsWidget
from ..widgets.playlist.widget import PlaylistWidget
from ..widgets.projection.bar import ProjectionBar
from ..widgets.quick_access_toolbar import QuickAccessToolbar
from ..widgets.sermon_theme_widget import SermonThemeWidget
from ..widgets.settings_widget import SettingsWidget
from ..widgets.songs_widget import SongsWidget
from ..widgets.timer_widget import TimerWidget
from .lazy_page_controller import (
    LazyPageContext,
    LazyPageController,
    LazyPageHandlers,
)
from .main_window_nav import (
    NAV_LABELS,
    SIDEBAR_SUBTITLE_SOURCE,
    SIDEBAR_TITLE_SOURCE,
    SWITCH_PROFILE_SOURCE,
)
from .navigation_controller import NavigationController

if TYPE_CHECKING:
    from ..core.media.browser_downloads import BrowserDownloadService
    from ..core.network.browser_images import BrowserImageFetchService


@dataclass(frozen=True, slots=True)
class MainWindowUiContext:
    parent: QWidget
    event_filter: QObject
    set_central_widget: Callable[[QWidget], None]
    active_profile_name: str
    translate: Callable[[str], str]
    lang_manager: Any
    notifications: Any
    profile_paths: Any
    runtime_paths: Any
    media_cache_manager: Any
    media_controller: Any
    screen_manager: Any
    obs_service: Any
    ndi_service: Any
    zoom_service: Any
    camera_service: Any
    obs_settings: Any
    zoom_settings: Any
    auto_share_settings: Any
    camera_settings: Any
    auto_key_settings: Any
    media_settings: Any
    meeting_schedule_settings: Any
    watched_folder_settings: Any
    yeartext_settings: Any
    yeartext_service_factory: Callable[[QObject], Any]
    background_song_settings: Any
    projection_playback_settings: Any
    background_song_service: Any
    timer_bridge: Any
    playlist_storage_paths: Any
    playlist_repository: Any
    meeting_tree_store: Any
    profile_media_store: Any
    jwpub_import_thread_factory: Any
    document_conversion_service: Any
    clip_fetch_thread_factory: Any
    cache_scan_session_factory: Any
    playlist_thumbnail_store: Any
    meeting_thumbnail_store: Any
    watched_folder_file_store: Any
    watched_folder_playlist_store: Any
    wifi_receive_server_factory: Callable[[QObject], Any]
    watched_folder_watcher_factory: Callable[[QObject], Any]
    playlist_cleanup_queue_factory: Callable[..., Any]
    jw_catalog_service_factory: Callable[[QObject], Any]
    jw_catalog_thumbnail_session_factory: Any
    jw_songs_store: Any
    jwpub_service_factory: Callable[[QObject], Any]
    memorial_service_factory: Callable[[QObject], Any]


@dataclass(frozen=True, slots=True)
class MainWindowUiHandlers:
    project_image: Callable[..., Any]
    project_video: Callable[..., Any]
    stop_projection: Callable[..., Any]
    project_tab_frame: Callable[..., Any]
    add_current_to_playlist: Callable[..., Any]
    add_downloaded_file_to_playlist: Callable[..., Any]
    report_download_failure: Callable[..., Any]
    play_cached_media: Callable[..., Any]
    wifi_media_received: Callable[..., Any]
    wifi_add_single: Callable[..., Any]
    wifi_add_all: Callable[..., Any]
    wifi_play: Callable[..., Any]
    monitor_manager_requested: Callable[..., Any]
    quick_obs_scene_change: Callable[..., Any]
    quick_obs_return_scene_change: Callable[..., Any]
    project_obs_stream: Callable[..., Any]
    project_camera_stream: Callable[..., Any]
    camera_selection_changed: Callable[..., Any]
    profile_switch_requested: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class MainWindowUiResources:
    stack: QStackedWidget
    lazy_pages: LazyPageController
    navigation: NavigationController
    right_col: QWidget
    projection_bar: ProjectionBar
    songs_widget: SongsWidget
    settings_widget: SettingsWidget
    timer_widget: TimerWidget
    clips_widget: ClipsWidget
    sermon_theme_widget: SermonThemeWidget
    playlist_widget: PlaylistWidget
    meetings_widget: MeetingsWidget
    quick_toolbar: QuickAccessToolbar
    sidebar_title_label: QLabel
    sidebar_subtitle_label: QLabel
    profile_avatar_button: ProfileAvatarButton
    nav_buttons: list[SidebarButton]
    nav_buttons_by_name: dict[str, SidebarButton]


@dataclass(frozen=True, slots=True)
class _PageResources:
    songs_widget: SongsWidget
    settings_widget: SettingsWidget
    timer_widget: TimerWidget
    clips_widget: ClipsWidget
    sermon_theme_widget: SermonThemeWidget
    playlist_widget: PlaylistWidget
    meetings_widget: MeetingsWidget


@dataclass(frozen=True, slots=True)
class _SidebarResources:
    frame: QFrame
    title_label: QLabel
    subtitle_label: QLabel
    profile_avatar_button: ProfileAvatarButton
    nav_buttons: list[SidebarButton]
    nav_buttons_by_name: dict[str, SidebarButton]


class MainWindowUiController:
    """Builds the main-window widget tree from explicit UI dependencies."""

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

    def __init__(
        self,
        context: MainWindowUiContext,
        handlers: MainWindowUiHandlers,
        *,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        browser_download_service_factory: Callable[[], BrowserDownloadService],
        browser_image_fetch_service_factory: Callable[[], BrowserImageFetchService],
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._media_info_queue_factory = media_info_queue_factory
        self._media_info_service_factory = media_info_service_factory
        self._browser_download_service_factory = browser_download_service_factory
        self._browser_image_fetch_service_factory = browser_image_fetch_service_factory

    @classmethod
    def nav_button_specs(cls) -> tuple[tuple[str, str, str, int], ...]:
        return cls._NAV_BUTTON_SPECS

    @classmethod
    def sidebar_layout_order(cls) -> tuple[str, ...]:
        return cls._SIDEBAR_LAYOUT_ORDER

    def build_ui(self) -> MainWindowUiResources:
        context = self._context
        central = QWidget()
        context.set_central_widget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        stack = QStackedWidget()
        stack.setObjectName("ContentArea")
        lazy_pages = self._build_lazy_pages(stack)

        nav_buttons: list[SidebarButton] = []
        projection_bar_ref: dict[str, ProjectionBar | None] = {"value": None}
        quick_toolbar_ref: dict[str, QuickAccessToolbar | None] = {"value": None}
        navigation = NavigationController(
            stack,
            lazy_pages,
            nav_buttons=lambda: nav_buttons,
            projection_bar=lambda: projection_bar_ref["value"],
            quick_toolbar=lambda: quick_toolbar_ref["value"],
        )

        pages = self._build_pages(stack, lazy_pages)
        sidebar = self._build_sidebar(navigation, nav_buttons)
        root.addWidget(sidebar.frame)

        right_col = QWidget()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(stack, stretch=1)

        bottom_bar, projection_bar = self._build_bottom_bar(right_col)
        projection_bar_ref["value"] = projection_bar
        right_layout.addWidget(bottom_bar)

        root.addWidget(right_col, stretch=1)

        quick_toolbar = self._build_quick_toolbar(
            right_col,
            navigation,
            pages.settings_widget,
        )
        quick_toolbar_ref["value"] = quick_toolbar
        self._prime_native_cursor_hosts(right_col, stack)
        right_col.installEventFilter(context.event_filter)

        return MainWindowUiResources(
            stack=stack,
            lazy_pages=lazy_pages,
            navigation=navigation,
            right_col=right_col,
            projection_bar=projection_bar,
            songs_widget=pages.songs_widget,
            settings_widget=pages.settings_widget,
            timer_widget=pages.timer_widget,
            clips_widget=pages.clips_widget,
            sermon_theme_widget=pages.sermon_theme_widget,
            playlist_widget=pages.playlist_widget,
            meetings_widget=pages.meetings_widget,
            quick_toolbar=quick_toolbar,
            sidebar_title_label=sidebar.title_label,
            sidebar_subtitle_label=sidebar.subtitle_label,
            profile_avatar_button=sidebar.profile_avatar_button,
            nav_buttons=nav_buttons,
            nav_buttons_by_name=sidebar.nav_buttons_by_name,
        )

    def _build_lazy_pages(self, stack: QStackedWidget) -> LazyPageController:
        context = self._context
        handlers = self._handlers
        return LazyPageController(
            LazyPageContext(
                parent=context.parent,
                stack=stack,
                lang_manager=context.lang_manager,
                notifications=context.notifications,
                profile_paths=context.profile_paths,
                media_cache_manager=context.media_cache_manager,
                profile_media_store=context.profile_media_store,
                jwpub_import_thread_factory=context.jwpub_import_thread_factory,
                document_conversion_service=context.document_conversion_service,
                cache_scan_session_factory=context.cache_scan_session_factory,
                wifi_receive_server_factory=context.wifi_receive_server_factory,
                browser_download_service_factory=(
                    self._browser_download_service_factory
                ),
                browser_image_fetch_service_factory=(
                    self._browser_image_fetch_service_factory
                ),
                media_info_service_factory=self._media_info_service_factory,
            ),
            LazyPageHandlers(
                project_image=handlers.project_image,
                project_video=handlers.project_video,
                stop_projection=handlers.stop_projection,
                project_tab_frame=handlers.project_tab_frame,
                add_current_to_playlist=handlers.add_current_to_playlist,
                add_downloaded_file_to_playlist=(
                    handlers.add_downloaded_file_to_playlist
                ),
                report_download_failure=handlers.report_download_failure,
                play_cached_media=handlers.play_cached_media,
                wifi_media_received=handlers.wifi_media_received,
                wifi_add_single=handlers.wifi_add_single,
                wifi_add_all=handlers.wifi_add_all,
                wifi_play=handlers.wifi_play,
            ),
        )

    def _build_pages(
        self,
        stack: QStackedWidget,
        lazy_pages: LazyPageController,
    ) -> _PageResources:
        context = self._context
        songs_widget = SongsWidget(
            context.lang_manager,
            context.media_cache_manager,
            context.jw_songs_store,
            context.runtime_paths.cache_dir,
            context.media_controller,
            parent=context.parent,
        )
        settings_widget = SettingsWidget(
            context.lang_manager,
            context.screen_manager,
            obs_service=context.obs_service,
            ndi_service=context.ndi_service,
            obs_settings=context.obs_settings,
            zoom_settings=context.zoom_settings,
            auto_share_settings=context.auto_share_settings,
            camera_settings=context.camera_settings,
            auto_key_settings=context.auto_key_settings,
            media_settings=context.media_settings,
            meeting_schedule_settings=context.meeting_schedule_settings,
            watched_folder_settings=context.watched_folder_settings,
            yeartext_settings=context.yeartext_settings,
            yeartext_service_factory=context.yeartext_service_factory,
            background_song_settings=context.background_song_settings,
            parent=context.parent,
        )
        timer_widget = TimerWidget(
            context.lang_manager,
            bridge=context.timer_bridge,
            parent=context.parent,
        )
        clips_widget = ClipsWidget(
            context.lang_manager,
            context.media_cache_manager,
            context.runtime_paths.cache_dir,
            context.clip_fetch_thread_factory,
            context.media_controller,
            parent=context.parent,
        )
        sermon_theme_widget = SermonThemeWidget(
            context.lang_manager,
            parent=context.parent,
        )
        watched_folder = settings_widget.get_watched_folder()
        playlist_widget = PlaylistWidget(
            context.lang_manager,
            media_ctrl=context.media_controller,
            watched_folder=watched_folder,
            notifications=context.notifications,
            profile_paths=context.profile_paths,
            document_conversion_service=context.document_conversion_service,
            storage_paths=context.playlist_storage_paths,
            playlist_repository=context.playlist_repository,
            profile_media_store=context.profile_media_store,
            jwpub_import_thread_factory=context.jwpub_import_thread_factory,
            playlist_thumbnail_store=context.playlist_thumbnail_store,
            watched_folder_file_store=context.watched_folder_file_store,
            watched_folder_playlist_store=context.watched_folder_playlist_store,
            watched_folder_watcher_factory=context.watched_folder_watcher_factory,
            playlist_cleanup_queue_factory=context.playlist_cleanup_queue_factory,
            media_cache_manager=context.media_cache_manager,
            jw_catalog_service_factory=context.jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory=(
                context.jw_catalog_thumbnail_session_factory
            ),
            jw_songs_store=context.jw_songs_store,
            media_info_queue_factory=self._media_info_queue_factory,
            parent=context.parent,
        )
        meetings_widget = MeetingsWidget(
            context.lang_manager,
            meeting_tree_store=context.meeting_tree_store,
            profile_media_store=context.profile_media_store,
            jwpub_import_thread_factory=context.jwpub_import_thread_factory,
            document_conversion_service=context.document_conversion_service,
            meeting_thumbnail_store=context.meeting_thumbnail_store,
            watched_folder_file_store=context.watched_folder_file_store,
            watched_folder_watcher_factory=context.watched_folder_watcher_factory,
            profile_paths=context.profile_paths,
            runtime_paths=context.runtime_paths,
            cache_manager=context.media_cache_manager,
            jw_catalog_service_factory=context.jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory=(
                context.jw_catalog_thumbnail_session_factory
            ),
            jw_songs_store=context.jw_songs_store,
            media_settings=context.media_settings,
            meeting_schedule_settings=context.meeting_schedule_settings,
            jwpub_service_factory=context.jwpub_service_factory,
            memorial_service_factory=context.memorial_service_factory,
            media_info_queue_factory=self._media_info_queue_factory,
            parent=context.parent,
        )
        meetings_widget.set_watched_folder(watched_folder)

        self._flush_orphaned_media_files()

        stack.addWidget(songs_widget)
        stack.addWidget(meetings_widget)
        stack.addWidget(lazy_pages.placeholder())
        stack.addWidget(clips_widget)
        stack.addWidget(timer_widget)
        stack.addWidget(sermon_theme_widget)
        stack.addWidget(settings_widget)
        stack.addWidget(playlist_widget)
        stack.addWidget(lazy_pages.placeholder())
        stack.addWidget(lazy_pages.placeholder())

        return _PageResources(
            songs_widget=songs_widget,
            settings_widget=settings_widget,
            timer_widget=timer_widget,
            clips_widget=clips_widget,
            sermon_theme_widget=sermon_theme_widget,
            playlist_widget=playlist_widget,
            meetings_widget=meetings_widget,
        )

    def _build_sidebar(
        self,
        navigation: NavigationController,
        shared_nav_buttons: list[SidebarButton],
    ) -> _SidebarResources:
        context = self._context
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(4)

        title_label = QLabel(context.translate(SIDEBAR_TITLE_SOURCE))
        title_label.setObjectName("SectionTitle")
        title_label.setStyleSheet(
            "background: transparent; font-size: 14px; font-weight: 700; "
            "padding: 4px 8px 2px 8px;"
        )
        subtitle_label = QLabel(context.translate(SIDEBAR_SUBTITLE_SOURCE))
        subtitle_label.setObjectName("SectionSubtitle")
        subtitle_label.setStyleSheet(
            "background: transparent; padding: 0 8px 12px 8px;"
        )

        profile_avatar_button = self._build_sidebar_header(
            title_label,
            subtitle_label,
        )
        nav_buttons_by_name, nav_buttons = self._build_nav_buttons(navigation)
        shared_nav_buttons.extend(nav_buttons)

        layout.addLayout(
            self._build_sidebar_header_layout(
                title_label,
                subtitle_label,
                profile_avatar_button,
            )
        )
        layout.addWidget(self._separator())
        layout.addSpacing(4)
        for attr_name in self._SIDEBAR_LAYOUT_ORDER:
            layout.addWidget(nav_buttons_by_name[attr_name])
        layout.addStretch()
        layout.addWidget(nav_buttons_by_name["nav_settings_btn"])

        navigation.switch_page(1)
        return _SidebarResources(
            frame=sidebar,
            title_label=title_label,
            subtitle_label=subtitle_label,
            profile_avatar_button=profile_avatar_button,
            nav_buttons=nav_buttons,
            nav_buttons_by_name=nav_buttons_by_name,
        )

    def _build_bottom_bar(self, right_col: QWidget) -> tuple[QFrame, ProjectionBar]:
        context = self._context
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        projection_bar = ProjectionBar(
            context.media_controller,
            playback_settings=context.projection_playback_settings,
            profile_paths=context.profile_paths,
            profile_media_store=context.profile_media_store,
            media_cache_dir=context.media_cache_manager.media_cache_dir,
            media_info_queue_factory=self._media_info_queue_factory,
            lang_manager=context.lang_manager,
            container=right_col,
        )
        projection_bar.setFixedHeight(48)
        projection_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        projection_bar.setStyleSheet(
            "QFrame#StatusBar { border: none; background: transparent; }"
        )
        layout.addWidget(projection_bar)
        return bar, projection_bar

    def _build_quick_toolbar(
        self,
        right_col: QWidget,
        navigation: NavigationController,
        settings_widget: SettingsWidget,
    ) -> QuickAccessToolbar:
        context = self._context
        handlers = self._handlers
        toolbar = QuickAccessToolbar(
            context.obs_service,
            context.zoom_service,
            context.camera_service,
            right_col,
            obs_settings=context.obs_settings,
            camera_settings=context.camera_settings,
            background_song_service=context.background_song_service,
        )
        toolbar.monitor_clicked.connect(handlers.monitor_manager_requested)
        toolbar.obs_scene_change.connect(handlers.quick_obs_scene_change)
        toolbar.obs_return_scene_change.connect(handlers.quick_obs_return_scene_change)
        toolbar.obs_stream_requested.connect(handlers.project_obs_stream)
        toolbar.obs_camera_stream_requested.connect(handlers.project_camera_stream)
        toolbar.camera_stream_requested.connect(handlers.project_camera_stream)
        toolbar.camera_selection_changed.connect(handlers.camera_selection_changed)
        toolbar.set_camera_enabled(settings_widget.get_camera_enabled())
        toolbar.show()
        toolbar.reposition()
        navigation.update_quick_toolbar_browser_style()
        return toolbar

    @staticmethod
    def _prime_native_cursor_hosts(
        right_col: QWidget,
        stack: QStackedWidget,
    ) -> None:
        for widget in (right_col, stack):
            widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            widget.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
            widget.winId()

    def _build_sidebar_header(
        self,
        _title_label: QLabel,
        _subtitle_label: QLabel,
    ) -> ProfileAvatarButton:
        profile_avatar_button = ProfileAvatarButton(
            self._context.active_profile_name,
        )
        profile_avatar_button.setToolTip(
            self._context.translate(SWITCH_PROFILE_SOURCE)
        )
        profile_avatar_button.clicked.connect(
            self._handlers.profile_switch_requested
        )
        return profile_avatar_button

    @staticmethod
    def _build_sidebar_header_layout(
        title_label: QLabel,
        subtitle_label: QLabel,
        profile_avatar_button: ProfileAvatarButton,
    ) -> QHBoxLayout:
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_col.addWidget(title_label)
        title_col.addWidget(subtitle_label)

        header_row.addLayout(title_col, 1)
        header_row.addWidget(
            profile_avatar_button,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        return header_row

    def _build_nav_buttons(
        self,
        navigation: NavigationController,
    ) -> tuple[dict[str, SidebarButton], list[SidebarButton]]:
        nav_buttons_by_name: dict[str, SidebarButton] = {}
        nav_buttons: list[SidebarButton] = []
        for attr_name, icon, label, page_index in self._NAV_BUTTON_SPECS:
            button = SidebarButton(icon, self._context.translate(label))
            button.clicked.connect(
                lambda _checked=False, index=page_index: navigation.switch_page(index)
            )
            nav_buttons_by_name[attr_name] = button
            nav_buttons.append(button)
        return nav_buttons_by_name, nav_buttons

    @staticmethod
    def _separator() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#21262d; border:none; margin: 4px 0;")
        return sep

    def _flush_orphaned_media_files(self) -> None:
        context = self._context
        storage_paths = context.playlist_storage_paths
        runtime_paths = context.runtime_paths
        meeting_tree_store = context.meeting_tree_store
        profile_paths = context.profile_paths
        flush_pending_deletions(storage_paths, meeting_tree_store)
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
