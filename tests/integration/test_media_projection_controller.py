from solin.controllers.media_projection_controller import (
    MediaProjectionContext,
    MediaProjectionController,
    MediaProjectionHandlers,
)
from solin.core.projection.application import ProjectionSession


class _NavigationStub:
    def __init__(self):
        self.stopped = 0

    def stop_browser_tab_projection(self):
        self.stopped += 1


class _ServiceStub:
    def __init__(self, events=None, name="media"):
        self.stopped = 0
        self.played = []
        self.paused = 0
        self.events = events
        self.name = name

    def stop(self):
        self.stopped += 1
        if self.events is not None:
            self.events.append((self.name, "stop"))

    def play_url(self, url):
        self.played.append(url)
        if self.events is not None:
            self.events.append((self.name, "play_url", url))

    def pause(self):
        self.paused += 1
        if self.events is not None:
            self.events.append((self.name, "pause"))


class _SettingsStub:
    def __init__(self):
        self.sjjm_announce_mode = False
        self.start_videos_paused = False

    def get_sjjm_announce_mode(self):
        return self.sjjm_announce_mode

    def get_start_videos_paused(self):
        return self.start_videos_paused


class _PreviewContentStub:
    def __init__(self):
        self.image_modes = []

    def set_image_mode(self, enabled):
        self.image_modes.append(enabled)


class _ProjectionBarStub:
    def __init__(self, events=None):
        self.events = events
        self.playlists = []
        self.playlist_indices = []
        self.videos = []
        self.images = []
        self.announcement_count = 0
        self.current_item = None
        self.expanded = False
        self.video_mode = False
        self.audio_mode = False
        self.projected_titles = []
        self.hidden_add_to_playlist = 0
        self.live_tab_modes = []
        self.tab_previews = []
        self.preview_content = _PreviewContentStub()
        self._playlist_items = []

    def set_playlist(self, items, playback_order=None, from_saved_playlist=False):
        self.playlists.append((items, playback_order, from_saved_playlist))
        self._playlist_items = items
        if self.events is not None:
            self.events.append(("proj_bar", "set_playlist", items))

    def set_playlist_index(self, index):
        self.playlist_indices.append(index)

    def activate_video(self, title, keep_expanded=False, is_audio=False):
        self.videos.append((title, keep_expanded, is_audio))
        self.video_mode = True
        self.audio_mode = is_audio
        if self.events is not None:
            self.events.append(("proj_bar", "activate_video", title, is_audio))

    def activate_image(self, title, image_data=None, keep_expanded=False):
        self.images.append((title, image_data, keep_expanded))
        self.video_mode = False
        self.audio_mode = False

    def begin_announcement_mode(self):
        self.announcement_count += 1
        if self.events is not None:
            self.events.append(("proj_bar", "begin_announcement_mode"))

    def current_playlist_item(self):
        return self.current_item

    def is_expanded(self):
        return self.expanded

    def is_video_mode(self):
        return self.video_mode

    def is_audio_mode(self):
        return self.audio_mode

    def set_projected_title(self, title):
        self.projected_titles.append(title)

    def hide_add_to_playlist_action(self):
        self.hidden_add_to_playlist += 1

    def set_live_tab_mode(self, enabled):
        self.live_tab_modes.append(enabled)

    def update_tab_live_preview(self, frame):
        self.tab_previews.append(frame)

    def playlist_items(self):
        return self._playlist_items


class _AutoKeyProjectionStub:
    def __init__(self, events=None):
        self.prepared = 0
        self.events = events

    def prepare_video_session(self):
        self.prepared += 1
        if self.events is not None:
            self.events.append(("auto_key", "prepare_video_session"))


class _ProjectionIntegrationsStub:
    def __init__(self):
        self.statuses = []

    def update_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))


class _ProjectionWindowStub:
    def __init__(self, events=None):
        self.events = events
        self.cleared = 0
        self.began_video = 0
        self.images = []
        self.pixmaps = []
        self.frames = []
        self.transforms = []
        self.instant_resets = 0

    def clear(self):
        self.cleared += 1
        if self.events is not None:
            self.events.append(("projection_window", "clear"))

    def begin_video(self):
        self.began_video += 1
        if self.events is not None:
            self.events.append(("projection_window", "begin_video"))

    def show_image_from_url_data(self, data):
        self.images.append(data)

    def show_image_from_pixmap(self, frame):
        self.pixmaps.append(frame)

    def update_frame(self, frame):
        self.frames.append(frame)

    def set_image_transform(self, zoom, norm_x, norm_y):
        self.transforms.append((zoom, norm_x, norm_y))

    def reset_image_transform_instant(self):
        self.instant_resets += 1


class _EditViewStub:
    def __init__(self):
        self._is_temp = True


class _PlaylistWidgetStub:
    def __init__(self):
        self._edit_view = _EditViewStub()


class _MeetingServiceStub:
    def __init__(self):
        self.resolved = {}

    def resolve_video(self, item):
        return self.resolved


class _WindowStub:
    def __init__(self):
        self.events = []
        self.projection_session = ProjectionSession()
        self.projection_session.set_tab_projection_active(True)
        self._navigation = _NavigationStub()
        self.media_ctrl = _ServiceStub(self.events, "media")
        self._ndi_service = _ServiceStub(self.events, "ndi")
        self._camera_service = _ServiceStub(self.events, "camera")
        self.proj_bar = _ProjectionBarStub(self.events)
        self.settings_widget = _SettingsStub()
        self._auto_key_projection = _AutoKeyProjectionStub(self.events)
        self._projection_integrations = _ProjectionIntegrationsStub()
        self.playlist_widget = _PlaylistWidgetStub()
        self.meeting_service = _MeetingServiceStub()
        self.windows = [
            _ProjectionWindowStub(self.events),
            _ProjectionWindowStub(self.events),
        ]

    def _all_windows(self):
        return self.windows

    def tr(self, text):
        return text


def _controller(window):
    return MediaProjectionController(
        MediaProjectionContext(
            projection_session=window.projection_session,
            projection_bar=window.proj_bar,
            media_controller=window.media_ctrl,
            ndi_service=window._ndi_service,
            camera_service=window._camera_service,
            projection_windows=window._all_windows,
            playlist_edit_is_temp=lambda: getattr(
                window.playlist_widget._edit_view,
                "_is_temp",
                False,
            ),
            meeting_service=lambda: window.meeting_service,
            dialog_parent=window,
            translate=window.tr,
            sjjm_announce_mode=window.settings_widget.get_sjjm_announce_mode,
            start_videos_paused=window.settings_widget.get_start_videos_paused,
        ),
        MediaProjectionHandlers(
            stop_browser_tab_projection=(
                window._navigation.stop_browser_tab_projection
            ),
            update_projection_status=(
                window._projection_integrations.update_status
            ),
            prepare_video_session=(
                window._auto_key_projection.prepare_video_session
            ),
        ),
    )


def test_project_video_classifies_audio_and_updates_status():
    window = _WindowStub()
    controller = _controller(window)

    controller.project_video("song.mp3", "Song")

    assert window.proj_bar.playlists == [
        ([{"url": "song.mp3", "title": "Song"}], None, False)
    ]
    assert window.proj_bar.videos == [("Song", False, True)]
    assert [projection_window.began_video for projection_window in window.windows] == [0, 0]
    assert window.media_ctrl.played == ["song.mp3"]
    assert window.projection_session.state == {"type": "video", "is_audio": True}
    assert window._projection_integrations.statuses == [
        ((True, "Song"), {"visual": False, "auto_keys_media": False})
    ]


def test_project_video_core_uses_announcement_mode_for_sjjm_video():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller._next_is_sjjm = True

    controller.project_video_core("video.mp4", "Video", keep_expanded=True)

    assert controller._next_is_sjjm is False
    assert window.proj_bar.videos == [("Video", True, False)]
    assert window.proj_bar.announcement_count == 1
    assert window._auto_key_projection.prepared == 1
    assert window.media_ctrl.played == ["video.mp4"]
    assert window.media_ctrl.paused == 0
    assert [projection_window.began_video for projection_window in window.windows] == [1, 1]
    assert window.events.index(("proj_bar", "begin_announcement_mode")) < (
        window.events.index(("media", "play_url", "video.mp4"))
    )
    assert ("media", "pause") not in window.events


def test_project_video_core_starts_regular_visual_videos_paused_when_enabled():
    window = _WindowStub()
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)

    controller.project_video_core("talk.mp4", "Talk")

    assert window.proj_bar.announcement_count == 0
    assert window.media_ctrl.played == ["talk.mp4"]
    assert window.media_ctrl.paused == 1
    assert window.events.index(("media", "play_url", "talk.mp4")) < (
        window.events.index(("media", "pause"))
    )
    assert window._auto_key_projection.prepared == 1
    assert [projection_window.began_video for projection_window in window.windows] == [1, 1]


def test_project_video_core_pauses_sjjm_video_when_announcement_mode_is_disabled():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = False
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller._next_is_sjjm = True

    controller.project_video_core("song.mp4", "Song")

    assert controller._next_is_sjjm is False
    assert window.proj_bar.announcement_count == 0
    assert window.media_ctrl.played == ["song.mp4"]
    assert window.media_ctrl.paused == 1


def test_project_video_core_ignores_announcement_for_non_sjjm_videos_but_still_pauses():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)

    controller.project_video_core("regular.mp4", "Regular")

    assert window.proj_bar.announcement_count == 0
    assert window.media_ctrl.played == ["regular.mp4"]
    assert window.media_ctrl.paused == 1


def test_project_video_core_never_start_pauses_audio_or_enters_song_announcement():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller._next_is_sjjm = True

    controller.project_video_core("song.mp3", "Song Audio", is_audio=True)

    assert controller._next_is_sjjm is False
    assert window.proj_bar.videos == [("Song Audio", False, True)]
    assert window.proj_bar.announcement_count == 0
    assert window.media_ctrl.played == ["song.mp3"]
    assert window.media_ctrl.paused == 0
    assert window._auto_key_projection.prepared == 0
    assert [projection_window.began_video for projection_window in window.windows] == [0, 0]


def test_on_sjjm_project_marks_only_the_next_video_for_announcement():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    controller = _controller(window)
    playlist = [{"url": "song.mp4", "title": "Song", "type": "video"}]

    controller.on_sjjm_project("song.mp4", "Song", playlist, "")

    assert controller._next_is_sjjm is False
    assert window.proj_bar.playlists == [(playlist, None, True)]
    assert window.proj_bar.announcement_count == 1
    assert window.media_ctrl.played == ["song.mp4"]
    assert window.media_ctrl.paused == 0

    controller.project_video_core("talk.mp4", "Talk")

    assert window.proj_bar.announcement_count == 1
    assert window.media_ctrl.played == ["song.mp4", "talk.mp4"]


def test_on_playlist_project_image_keeps_playlist_and_saved_source(tmp_path):
    window = _WindowStub()
    window.proj_bar.expanded = True
    controller = _controller(window)
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(b"image-data")
    playlist = [{"url": str(image_path), "title": "Slide", "type": "image"}]

    controller.on_playlist_project(str(image_path), "Slide", playlist, "random")

    assert window.proj_bar.playlists == [(playlist, "random", True)]
    assert window.proj_bar.images == [("Slide", b"image-data", True)]
    assert [projection_window.images for projection_window in window.windows] == [
        [b"image-data"],
        [b"image-data"],
    ]
    assert window.projection_session.state == {
        "type": "image",
        "data": b"image-data",
        "transform": (1.0, 0.0, 0.0),
    }


def test_project_tab_frame_initializes_live_tab_once():
    window = _WindowStub()
    window.projection_session.set_tab_projection_active(False)
    controller = _controller(window)
    frame = object()

    controller.project_tab_frame(frame)
    controller.project_tab_frame(frame)

    assert window.projection_session.tab_projection_active is True
    assert window.proj_bar.playlists == [([], None, False)]
    assert window.proj_bar.images[0][0] == "Browser — Live Tab"
    assert window.proj_bar.hidden_add_to_playlist == 1
    assert window.proj_bar.live_tab_modes == [True]
    assert window.proj_bar.tab_previews == [frame, frame]
    assert [projection_window.pixmaps for projection_window in window.windows] == [
        [frame, frame],
        [frame, frame],
    ]


def test_frame_and_image_transform_helpers_respect_projection_modes():
    window = _WindowStub()
    controller = _controller(window)

    window.proj_bar.video_mode = True
    controller.distribute_frame("frame-1")
    window.proj_bar.audio_mode = True
    controller.distribute_frame("frame-2")
    controller.on_image_apply_transform(1.5, 0.2, 0.3)
    controller.on_image_reset_transform()
    controller.on_image_reset_transform_instant()

    assert [projection_window.frames for projection_window in window.windows] == [
        ["frame-1"],
        ["frame-1"],
    ]
    assert [projection_window.transforms for projection_window in window.windows] == [
        [(1.5, 0.2, 0.3), (1.0, 0.0, 0.0)],
        [(1.5, 0.2, 0.3), (1.0, 0.0, 0.0)],
    ]
    assert [projection_window.instant_resets for projection_window in window.windows] == [1, 1]


def test_image_transform_is_persisted_in_projection_state():
    window = _WindowStub()
    controller = _controller(window)
    window.projection_session.set_state(
        {"type": "image", "data": b"x", "transform": (1.0, 0.0, 0.0)}
    )

    controller.on_image_apply_transform(2.0, 0.1, -0.2)
    assert window.projection_session.state["transform"] == (2.0, 0.1, -0.2)

    controller.on_image_reset_transform()
    assert window.projection_session.state["transform"] == (1.0, 0.0, 0.0)


def test_sermon_theme_transform_is_persisted_in_projection_state():
    window = _WindowStub()
    controller = _controller(window)
    window.projection_session.set_state({
        "type": "sermon_theme",
        "text": "t",
        "subtitle": "s",
        "transform": (1.0, 0.0, 0.0),
    })

    controller.on_image_apply_transform(1.4, 0.0, 0.1)

    assert window.projection_session.state["transform"] == (1.4, 0.0, 0.1)


def test_image_transform_not_persisted_when_state_is_not_image():
    window = _WindowStub()
    controller = _controller(window)
    window.projection_session.set_state({"type": "video", "is_audio": False})

    controller.on_image_apply_transform(2.0, 0.1, -0.2)

    assert "transform" not in window.projection_session.state


def test_title_metadata_only_updates_video_mode():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_title_from_metadata("Ignored")
    window.proj_bar.video_mode = True
    controller.on_title_from_metadata("Resolved")

    assert window.proj_bar.projected_titles == ["Resolved"]


def test_project_media_at_index_sets_playlist_index_for_images(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "second.png"
    image_path.write_bytes(b"second")
    playlist = [
        {"url": "video.mp4", "title": "Video", "type": "video"},
        {"url": str(image_path), "title": "Second", "type": "image"},
    ]

    controller.project_media_at_index(playlist, index=1, keep_expanded=True)

    assert window.proj_bar.playlists == [(playlist, None, False)]
    assert window.proj_bar.playlist_indices == [1]
    assert window.proj_bar.images == [("Second", b"second", True)]


def test_media_projection_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
