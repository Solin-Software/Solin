from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MainWindowSignalSources:
    """Qt objects that publish signals consumed by the main-window shell."""

    library_widget: Any
    meetings_widget: Any
    timer_widget: Any
    talk_theme_widget: Any
    playlist_widget: Any
    projection_bar: Any
    media_controller: Any
    playback_protection: Any
    screen_manager: Any
    language_manager: Any
    settings_widget: Any
    obs_service: Any
    ndi_service: Any
    auto_share_finished: Any
    media_countdown_automation: Any
    camera_service: Any | None = None


@dataclass(frozen=True, slots=True)
class MainWindowSignalHandlers:
    """Explicit destinations for signals published by the main-window shell."""

    media_projection: Any
    timer_projection: Any
    playlist_imports: Any
    media_destinations: Any
    auto_key_projection: Any
    projection_stop: Any
    projection_targets: Any
    language_controller: Any
    live_integrations: Any
    background_song_service: Any
    projection_integrations: Any
    open_meeting_schedule_settings: Any
    apply_theme: Any | None = None
    timer_output: Any | None = None
    timer_bridge: Any | None = None


class SignalConnectionController:
    """Wires shell signals without using MainWindow as a service locator."""

    def __init__(
        self,
        sources: MainWindowSignalSources,
        handlers: MainWindowSignalHandlers,
    ) -> None:
        self._sources = sources
        self._handlers = handlers

    def _refresh_timer_monitors(self) -> None:
        bridge = self._handlers.timer_bridge
        if bridge is not None:
            bridge.refreshMonitors()

    def connect_signals(self) -> None:
        sources = self._sources
        handlers = self._handlers
        media_projection = handlers.media_projection
        timer_projection = handlers.timer_projection
        projection_bar = sources.projection_bar
        media_controller = sources.media_controller
        live_integrations = handlers.live_integrations

        sources.library_widget.project_media_signal.connect(self._project_library_media)
        sources.library_widget.play_cached_media_signal.connect(media_projection.on_cache_play)
        sources.meetings_widget.project_media.connect(media_projection.on_meeting_media_project)
        sources.meetings_widget.media_destination_requested.connect(
            handlers.media_destinations.route
        )
        sources.meetings_widget.set_as_idle_requested.connect(
            handlers.projection_targets.request_idle_media
        )
        sources.timer_widget.project_timer_signal.connect(timer_projection.start_timer)
        sources.timer_widget.meeting_schedule_requested.connect(
            handlers.open_meeting_schedule_settings
        )
        sources.media_countdown_automation.countdown_requested.connect(
            timer_projection.start_automatic_timer
        )
        sources.media_countdown_automation.automatic_stop_requested.connect(
            handlers.projection_stop.stop_any
        )
        sources.talk_theme_widget.set_projection_handler(media_projection.project_generated_image)

        sources.playlist_widget.project_video_signal.connect(media_projection.on_playlist_project)
        sources.playlist_widget.media_destination_requested.connect(
            handlers.media_destinations.route
        )
        sources.playlist_widget.set_as_idle_requested.connect(
            handlers.projection_targets.request_idle_media
        )

        sources.playlist_widget.project_image_signal.connect(media_projection.project_image_bytes)
        projection_bar.source_duration_discovered.connect(
            sources.playlist_widget.record_source_duration
        )

        projection_bar.add_to_destination_requested.connect(
            handlers.media_destinations.route_projected_media
        )
        projection_bar.send_to_temp_playlist_requested.connect(
            handlers.playlist_imports.send_to_temp_playlist
        )

        media_controller.frame_ready.connect(media_projection.distribute_frame)
        media_controller.state_changed.connect(handlers.auto_key_projection.on_media_state)
        media_controller.cover_art_changed.connect(projection_bar.set_cover_art)
        media_controller.title_from_metadata.connect(media_projection.on_title_from_metadata)

        projection_bar.stop_requested.connect(handlers.projection_stop.stop_any)
        projection_bar.seek_requested.connect(sources.playback_protection.request_seek)
        projection_bar.toggle_requested.connect(media_controller.toggle_play_pause)
        projection_bar.volume_changed.connect(media_controller.set_volume)
        projection_bar.timer_updated.connect(timer_projection.on_timer_update_proj)
        projection_bar.timer_blink.connect(timer_projection.on_timer_blink_proj)
        projection_bar.play_next_requested.connect(media_projection.project_next_auto)
        projection_bar.playlist_navigate.connect(media_projection.on_playlist_navigate)
        projection_bar.image_apply_transform.connect(media_projection.on_image_apply_transform)
        projection_bar.image_apply_transform_instant.connect(
            media_projection.on_image_apply_transform_instant
        )
        projection_bar.image_reset_transform.connect(media_projection.on_image_reset_transform)

        sources.screen_manager.screens_changed.connect(
            handlers.projection_targets.on_screens_changed
        )
        if handlers.timer_output is not None:
            sources.screen_manager.screens_changed.connect(handlers.timer_output.on_screens_changed)
            sources.screen_manager.screens_changed.connect(self._refresh_timer_monitors)

        sources.language_manager.language_changed.connect(
            handlers.language_controller.change_language
        )
        settings = sources.settings_widget
        settings.general.yearly_text_changed.connect(handlers.projection_targets.apply_yearly_text)
        settings.general.yearly_text_changed.connect(projection_bar.set_yearly_text)
        settings.general.watched_folder_changed.connect(sources.playlist_widget.set_watched_folder)
        settings.general.watched_folder_changed.connect(sources.meetings_widget.set_watched_folder)
        settings.integrations.zoom_enabled_toggled.connect(live_integrations.on_zoom_settings_enabled_toggled)
        settings.integrations.zoom_participants_toggled.connect(live_integrations.on_zoom_settings_parts_toggled)
        settings.integrations.obs_stream_config_changed.connect(
            live_integrations.refresh_obs_stream_availability
        )
        if sources.camera_service is not None:
            settings.integrations.camera_enabled_toggled.connect(
                live_integrations.on_camera_settings_enabled_toggled
            )
        settings.general.background_song_toggled.connect(
            handlers.background_song_service.set_enabled
        )
        settings.general.meeting_schedule_changed.connect(
            handlers.background_song_service.reload_settings
        )
        settings.general.meeting_schedule_changed.connect(
            sources.media_countdown_automation.reload_schedule
        )
        if handlers.apply_theme is not None:
            settings.general.theme_changed.connect(handlers.apply_theme)

        sources.obs_service.state_changed.connect(live_integrations.on_obs_state_changed)
        sources.obs_service.current_scene_changed.connect(live_integrations.on_obs_scene_changed)
        sources.obs_service.scenes_updated.connect(
            lambda _: live_integrations.refresh_obs_btn_availability()
        )
        sources.obs_service.scenes_updated.connect(live_integrations.on_obs_scenes_updated)

        sources.ndi_service.frame_ready.connect(live_integrations.on_obs_ndi_frame)
        sources.ndi_service.error.connect(live_integrations.on_obs_ndi_error)
        sources.ndi_service.stopped.connect(live_integrations.on_obs_ndi_stopped)

        if sources.camera_service is not None:
            sources.camera_service.frame_ready.connect(live_integrations.on_camera_frame)
            sources.camera_service.error.connect(live_integrations.on_camera_error)
            sources.camera_service.stopped.connect(live_integrations.on_camera_stopped)

        projection_bar.obs_scene_toggle_requested.connect(live_integrations.on_obs_scene_toggle)
        projection_bar.set_as_idle_requested.connect(
            handlers.projection_targets.on_idle_media_changed
        )
        sources.auto_share_finished.connect(handlers.projection_integrations.on_auto_share_finished)

    def _project_library_media(
        self,
        section: str,
        url: str,
        title: str,
        playlist: object,
        playback_order: str,
    ) -> None:
        media_projection = self._handlers.media_projection
        if section == "songs":
            media_projection.on_sjjm_project(url, title, playlist, playback_order)
        elif section == "clips":
            media_projection.on_song_project(url, title, playlist, playback_order)
