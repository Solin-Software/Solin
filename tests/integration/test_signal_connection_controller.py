from types import SimpleNamespace

from solin.controllers.signal_connection_controller import (
    MainWindowSignalHandlers,
    MainWindowSignalSources,
    SignalConnectionController,
)


class _Signal:
    registry = []

    def __init__(self, name):
        self.name = name
        self.connected = []
        self.registry.append(self)

    def connect(self, callback):
        self.connected.append(callback)


def _slot(name):
    def _callback(*_args, **_kwargs):
        return None

    _callback.__name__ = name
    return _callback


def _signal_namespace(prefix, *names):
    return SimpleNamespace(**{
        name: _Signal(f"{prefix}.{name}") for name in names
    })


class _WindowStub:
    def __init__(self):
        self.songs_widget = _signal_namespace("songs", "project_video_signal")
        self.meetings_widget = _signal_namespace("meetings", "project_media")
        self.clips_widget = _signal_namespace("clips", "project_video_signal")
        self.timer_widget = _signal_namespace(
            "timer",
            "project_timer_signal",
            "meeting_schedule_requested",
        )
        self.talk_theme_projection_handler = None
        self.talk_theme_widget = SimpleNamespace(
            set_projection_handler=self._set_talk_theme_projection_handler
        )
        self.playlist_widget = _signal_namespace(
            "playlist",
            "project_video_signal",
            "project_image_signal",
        )
        self.playlist_widget.set_watched_folder = _slot("playlist_set_watched_folder")
        self.playlist_widget.record_source_duration = _slot("record_source_duration")

        self.proj_bar = _signal_namespace(
            "projection_bar",
            "add_to_destination_requested",
            "send_to_temp_playlist_requested",
            "source_duration_discovered",
            "stop_requested",
            "seek_requested",
            "toggle_requested",
            "volume_changed",
            "timer_updated",
            "timer_blink",
            "play_next_requested",
            "playlist_navigate",
            "image_apply_transform",
            "image_apply_transform_instant",
            "image_reset_transform",
            "obs_scene_toggle_requested",
            "set_as_idle_requested",
        )
        self.proj_bar.set_cover_art = _slot("set_cover_art")
        self.proj_bar.set_yearly_text = _slot("set_yearly_text")

        self.media_ctrl = _signal_namespace(
            "media_ctrl",
            "frame_ready",
            "state_changed",
            "cover_art_changed",
            "title_from_metadata",
        )
        self.media_ctrl.seek = _slot("seek")
        self.media_ctrl.toggle_play_pause = _slot("toggle_play_pause")
        self.media_ctrl.set_volume = _slot("set_volume")
        self.playback_protection = SimpleNamespace(request_seek=_slot("protected_seek"))

        self.screen_mgr = _signal_namespace("screen_mgr", "screens_changed")
        self.lang = _signal_namespace("lang", "language_changed")
        self.settings_widget = _signal_namespace(
            "settings",
            "yearly_text_changed",
            "watched_folder_changed",
            "zoom_enabled_toggled",
            "zoom_participants_toggled",
            "obs_stream_config_changed",
            "camera_enabled_toggled",
            "background_song_toggled",
            "meeting_schedule_changed",
        )
        self.meetings_widget.set_watched_folder = _slot("meetings_set_watched_folder")

        self._obs_service = _signal_namespace(
            "obs",
            "state_changed",
            "current_scene_changed",
            "scenes_updated",
        )
        self._ndi_service = _signal_namespace("ndi", "frame_ready", "error", "stopped")
        self._camera_service = _signal_namespace(
            "camera",
            "frame_ready",
            "error",
            "stopped",
        )
        self._background_song_service = SimpleNamespace(
            set_enabled=_slot("background_song_enabled"),
            reload_settings=_slot("background_song_reload_settings"),
        )
        self._media_countdown_automation = _signal_namespace(
            "media_countdown_automation",
            "countdown_requested",
            "automatic_stop_requested",
        )
        self._media_countdown_automation.reload_schedule = _slot(
            "media_countdown_reload_schedule"
        )
        self._auto_share_finished = _Signal("auto_share_finished")

        self._playlist_imports = SimpleNamespace(
            send_to_temp_playlist=_slot("send_to_temp_playlist"),
        )
        self._media_destinations = SimpleNamespace(
            route_projected_media=_slot("route_projected_media"),
        )
        self._auto_key_projection = SimpleNamespace(on_media_state=_slot("on_media_state"))
        self._projection_targets = SimpleNamespace(
            on_screens_changed=_slot("on_screens_changed"),
            apply_yearly_text=_slot("apply_yearly_text"),
            on_idle_media_changed=_slot("on_idle_media_changed"),
        )
        self._language_controller = SimpleNamespace(
            change_language=_slot("change_language"),
        )
        self._live_integrations = SimpleNamespace(
            on_zoom_settings_enabled_toggled=_slot("zoom_enabled"),
            on_zoom_settings_parts_toggled=_slot("zoom_parts"),
            refresh_obs_stream_availability=_slot("refresh_obs_stream"),
            on_camera_settings_enabled_toggled=_slot("camera_enabled"),
            on_obs_state_changed=_slot("obs_state"),
            on_obs_scene_changed=_slot("obs_scene"),
            refresh_obs_btn_availability=_slot("refresh_obs_button"),
            on_obs_scenes_updated=_slot("obs_scenes_updated"),
            on_obs_ndi_frame=_slot("ndi_frame"),
            on_obs_ndi_error=_slot("ndi_error"),
            on_obs_ndi_stopped=_slot("ndi_stopped"),
            on_camera_frame=_slot("camera_frame"),
            on_camera_error=_slot("camera_error"),
            on_camera_stopped=_slot("camera_stopped"),
            on_obs_scene_toggle=_slot("obs_scene_toggle"),
        )
        self._projection_integrations = SimpleNamespace(
            on_auto_share_finished=_slot("auto_share_finished"),
        )

        self._media_projection = SimpleNamespace(
            on_sjjm_project=_slot("on_sjjm_project"),
            on_meeting_media_project=_slot("on_meeting_media_project"),
            on_song_project=_slot("on_song_project"),
            on_playlist_project=_slot("on_playlist_project"),
            project_image_bytes=_slot("project_image_bytes"),
            project_generated_image=_slot("project_generated_image"),
            distribute_frame=_slot("distribute_frame"),
            on_title_from_metadata=_slot("title_from_metadata"),
            project_next_auto=_slot("project_next"),
            on_playlist_navigate=_slot("playlist_navigate"),
            on_image_apply_transform=_slot("image_apply"),
            on_image_apply_transform_instant=_slot("image_apply_instant"),
            on_image_reset_transform=_slot("image_reset"),
        )
        self._timer_projection = SimpleNamespace(
            start_timer=_slot("start_timer"),
            start_automatic_timer=_slot("start_automatic_timer"),
            on_timer_update_proj=_slot("timer_update"),
            on_timer_blink_proj=_slot("timer_blink"),
        )
        self._projection_stop = SimpleNamespace(stop_any=_slot("stop_any"))

    def _set_talk_theme_projection_handler(self, handler):
        self.talk_theme_projection_handler = handler


def _sources(window):
    return MainWindowSignalSources(
        songs_widget=window.songs_widget,
        meetings_widget=window.meetings_widget,
        clips_widget=window.clips_widget,
        timer_widget=window.timer_widget,
        talk_theme_widget=window.talk_theme_widget,
        playlist_widget=window.playlist_widget,
        projection_bar=window.proj_bar,
        media_controller=window.media_ctrl,
        playback_protection=window.playback_protection,
        screen_manager=window.screen_mgr,
        language_manager=window.lang,
        settings_widget=window.settings_widget,
        obs_service=window._obs_service,
        ndi_service=window._ndi_service,
        camera_service=window._camera_service,
        auto_share_finished=window._auto_share_finished,
        media_countdown_automation=window._media_countdown_automation,
    )


def _handlers(window, *, timer_output=None, timer_bridge=None):
    return MainWindowSignalHandlers(
        media_projection=window._media_projection,
        timer_projection=window._timer_projection,
        playlist_imports=window._playlist_imports,
        media_destinations=window._media_destinations,
        auto_key_projection=window._auto_key_projection,
        projection_stop=window._projection_stop,
        projection_targets=window._projection_targets,
        language_controller=window._language_controller,
        live_integrations=window._live_integrations,
        background_song_service=window._background_song_service,
        projection_integrations=window._projection_integrations,
        open_meeting_schedule_settings=_slot("open_meeting_schedule_settings"),
        timer_output=timer_output,
        timer_bridge=timer_bridge,
    )


def test_connect_signals_wires_expected_signal_graph():
    _Signal.registry = []
    window = _WindowStub()
    controller = SignalConnectionController(_sources(window), _handlers(window))

    controller.connect_signals()

    total_connections = sum(len(signal.connected) for signal in _Signal.registry)
    assert total_connections == 53
    assert window.songs_widget.project_video_signal.connected == [
        window._media_projection.on_sjjm_project
    ]
    assert window.settings_widget.watched_folder_changed.connected == [
        window.playlist_widget.set_watched_folder,
        window.meetings_widget.set_watched_folder,
    ]
    assert window.settings_widget.yearly_text_changed.connected == [
        window._projection_targets.apply_yearly_text,
        window.proj_bar.set_yearly_text,
    ]
    assert window.proj_bar.stop_requested.connected == [
        window._projection_stop.stop_any
    ]
    assert window.proj_bar.seek_requested.connected == [
        window.playback_protection.request_seek
    ]
    assert window.proj_bar.source_duration_discovered.connected == [
        window.playlist_widget.record_source_duration
    ]
    assert window.settings_widget.background_song_toggled.connected == [
        window._background_song_service.set_enabled
    ]
    assert window.settings_widget.meeting_schedule_changed.connected == [
        window._background_song_service.reload_settings,
        window._media_countdown_automation.reload_schedule,
    ]
    assert window.timer_widget.meeting_schedule_requested.connected == [
        controller._handlers.open_meeting_schedule_settings
    ]
    assert window._media_countdown_automation.countdown_requested.connected == [
        window._timer_projection.start_automatic_timer
    ]
    assert (
        window.talk_theme_projection_handler
        is window._media_projection.project_generated_image
    )
    assert window._media_countdown_automation.automatic_stop_requested.connected == [
        window._projection_stop.stop_any
    ]
    assert window._auto_share_finished.connected == [
        window._projection_integrations.on_auto_share_finished
    ]
    assert len(window._obs_service.scenes_updated.connected) == 2


def test_controller_uses_explicit_endpoints_instead_of_main_window():
    window = _WindowStub()

    controller = SignalConnectionController(_sources(window), _handlers(window))

    assert not hasattr(controller, "_window")


def test_screen_changes_refresh_timer_output_and_monitor_bridge():
    window = _WindowStub()
    timer_output = SimpleNamespace(on_screens_changed=_slot("timer_screens_changed"))
    refresh_calls = []
    timer_bridge = SimpleNamespace(
        refreshMonitors=lambda: refresh_calls.append("refresh")
    )
    controller = SignalConnectionController(
        _sources(window),
        _handlers(
            window,
            timer_output=timer_output,
            timer_bridge=timer_bridge,
        ),
    )

    controller.connect_signals()
    window.screen_mgr.screens_changed.connected[-1]()

    assert window.screen_mgr.screens_changed.connected[:2] == [
        window._projection_targets.on_screens_changed,
        timer_output.on_screens_changed,
    ]
    assert refresh_calls == ["refresh"]
