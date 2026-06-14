from __future__ import annotations


class SignalConnectionController:
    """Wires MainWindow signals after widgets and controllers are created."""

    def __init__(self, window) -> None:
        self._window = window

    def _refresh_timer_monitors(self) -> None:
        bridge = getattr(self._window.timer_widget, "_bridge", None)
        if bridge is not None:
            bridge.refreshMonitors()

    def connect_signals(self) -> None:
        window = self._window

        window.songs_widget.project_video_signal.connect(window._on_sjjm_project)
        window.meetings_widget.project_media.connect(window._on_meeting_media_project)
        window.clips_widget.project_video_signal.connect(window._on_song_project)
        window.timer_widget.project_timer_signal.connect(window._start_timer)
        window.sermon_theme_widget.project_theme_signal.connect(
            window._project_sermon_theme
        )

        window.playlist_widget.project_video_signal.connect(window._on_playlist_project)
        window.playlist_widget.project_image_signal.connect(window._project_image_bytes)

        window.proj_bar.add_to_playlist_requested.connect(
            window._playlist_imports.add_current_to_playlist
        )
        window.proj_bar.send_to_temp_playlist_requested.connect(
            window._playlist_imports.send_to_temp_playlist
        )

        window.media_ctrl.frame_ready.connect(window._distribute_frame)
        window.media_ctrl.state_changed.connect(window._auto_key_projection.on_media_state)
        window.media_ctrl.cover_art_changed.connect(window.proj_bar.set_cover_art)
        window.media_ctrl.title_from_metadata.connect(window._on_title_from_metadata)

        window.proj_bar.stop_requested.connect(window._stop_any)
        window.proj_bar.seek_requested.connect(window.media_ctrl.seek)
        window.proj_bar.toggle_requested.connect(window.media_ctrl.toggle_play_pause)
        window.proj_bar.volume_changed.connect(window.media_ctrl.set_volume)
        window.proj_bar.timer_updated.connect(window._on_timer_update_proj)
        window.proj_bar.timer_blink.connect(window._on_timer_blink_proj)
        window.proj_bar.play_next_requested.connect(window._project_next_auto)
        window.proj_bar.playlist_navigate.connect(window._on_playlist_navigate)
        window.proj_bar.image_apply_transform.connect(window._on_image_apply_transform)
        window.proj_bar.image_reset_transform.connect(window._on_image_reset_transform)
        window.proj_bar.image_reset_transform_instant.connect(
            window._on_image_reset_transform_instant
        )

        window.screen_mgr.screens_changed.connect(
            window._projection_targets.on_screens_changed
        )
        # Keep the timer clock windows + the Timer tab's monitor grid in sync
        # when monitors are hot-plugged.
        timer_output = getattr(window, "_timer_output", None)
        if timer_output is not None:
            window.screen_mgr.screens_changed.connect(timer_output.on_screens_changed)
            window.screen_mgr.screens_changed.connect(self._refresh_timer_monitors)

        window.lang.language_changed.connect(window._language_controller.change_language)
        window.settings_widget.yearly_text_changed.connect(
            window._projection_targets.apply_yearly_text
        )
        window.settings_widget.watched_folder_changed.connect(
            window.playlist_widget.set_watched_folder
        )
        window.settings_widget.watched_folder_changed.connect(
            window.meetings_widget.set_watched_folder
        )
        window.settings_widget.zoom_enabled_toggled.connect(
            window._live_integrations.on_zoom_settings_enabled_toggled
        )
        window.settings_widget.zoom_participants_toggled.connect(
            window._live_integrations.on_zoom_settings_parts_toggled
        )
        window.settings_widget.obs_stream_config_changed.connect(
            window._live_integrations.refresh_obs_stream_availability
        )
        window.settings_widget.camera_enabled_toggled.connect(
            window._live_integrations.on_camera_settings_enabled_toggled
        )
        window.settings_widget.background_song_toggled.connect(
            window._background_song_service.set_enabled
        )
        window.settings_widget.meeting_schedule_changed.connect(
            window._background_song_service.reload_settings
        )

        window._obs_service.state_changed.connect(
            window._live_integrations.on_obs_state_changed
        )
        window._obs_service.current_scene_changed.connect(
            window._live_integrations.on_obs_scene_changed
        )
        window._obs_service.scenes_updated.connect(
            lambda _: window._live_integrations.refresh_obs_btn_availability()
        )
        window._obs_service.scenes_updated.connect(
            window._live_integrations.on_obs_scenes_updated
        )

        window._ndi_service.frame_ready.connect(window._live_integrations.on_obs_ndi_frame)
        window._ndi_service.error.connect(window._live_integrations.on_obs_ndi_error)
        window._ndi_service.stopped.connect(window._live_integrations.on_obs_ndi_stopped)

        window._camera_service.frame_ready.connect(window._live_integrations.on_camera_frame)
        window._camera_service.error.connect(window._live_integrations.on_camera_error)
        window._camera_service.stopped.connect(window._live_integrations.on_camera_stopped)

        window.proj_bar.obs_scene_toggle_requested.connect(
            window._live_integrations.on_obs_scene_toggle
        )
        window.proj_bar.set_as_idle_requested.connect(
            window._projection_targets.on_idle_media_changed
        )
        window._auto_share_finished.connect(
            window._projection_integrations.on_auto_share_finished
        )
