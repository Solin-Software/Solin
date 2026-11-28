from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage

from solin.controllers.media_projection_controller import (
    MediaProjectionContext,
    MediaProjectionController,
    MediaProjectionHandlers,
)
from solin.controllers.projection_playback_operations import ProjectionPlaybackOperations
from solin.core.foundation.constants import ORDER_NEXT, ORDER_OFF, ORDER_RANDOM
from solin.core.meetings.models import MeetingMedia
from solin.core.projection.application import ProjectionSession
from solin.core.projection.image_framing import ImageTransform
from solin.core.projection.result import ProjectionResultStatus


class _UncomparableBytes(bytes):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("image bytes must not be compared")

    def __ne__(self, other: object) -> bool:
        raise AssertionError("image bytes must not be compared")


class _NavigationStub:
    def __init__(self):
        self.stopped = 0

    def stop_browser_tab_projection(self):
        self.stopped += 1


class _ServiceStub(QObject):
    volume_changed = Signal(float)
    duration_changed = Signal(int)
    position_changed = Signal(int)

    def set_volume(self, volume):
        self.volume = volume
        self.volume_changed.emit(volume)

    def set_playback_rate(self, rate):
        self.playback_rate = rate

    def set_audio_suppressed(self, suppressed):
        self.events.append(("media", "audio_suppressed", suppressed))


    def __init__(self, events=None, name="media"):
        super().__init__()
        self.position = 0
        self.is_ended = False
        self.stopped = 0
        self.played = []
        self.paused = 0
        self.resumed = 0
        self.replayed = 0
        self.requests = []
        self.events = events
        self.name = name

    def stop(self):
        self.stopped += 1
        if self.events is not None:
            self.events.append((self.name, "stop"))

    @contextmanager
    def replace_playback(self):
        self.stop()
        yield

    def start_playback(self, request):
        self.requests.append(request)
        self.played.append(request.source)
        if self.events is not None:
            self.events.append((self.name, "start_playback", request.source))

    def pause(self):
        self.paused += 1
        if self.events is not None:
            self.events.append((self.name, "pause"))

    def play(self):
        self.resumed += 1

    def replay(self):
        self.replayed += 1

    def seek(self, position):
        self.position = position


class _SettingsStub:
    def __init__(self):
        self.sjjm_announce_mode = False
        self.start_videos_paused = False

    def get_sjjm_announce_mode(self):
        return self.sjjm_announce_mode

    def get_start_videos_paused(self):
        return self.start_videos_paused


class _ProtectionStub(QObject):
    lockedChanged = Signal()

    def __init__(self):
        super().__init__()
        self.locked = False
        self.blocked = 0

    def allow_manual_projection_change(self, *, notify=True):
        if not self.locked:
            return True
        if notify:
            self.blocked += 1
        return False


class _PreviewContentStub:
    def __init__(self):
        self.image_modes = []

    def set_image_mode(self, enabled):
        self.image_modes.append(enabled)


class _ProjectionBarStub:
    def __init__(self, events=None):
        self.events = events
        self.videos = []
        self.images = []
        self.expanded = False
        self.video_mode = False
        self.audio_mode = False
        self.projected_titles = []
        self.hidden_add_to_destination = 0
        self.live_tab_modes = []
        self.tab_previews = []
        self.initial_transforms = []
        self.projected_image_transforms = []
        self.image_activation_options = []
        self.preview_content = _PreviewContentStub()

    def activate_video(self, title, keep_expanded=False, is_audio=False):
        self.videos.append((title, keep_expanded, is_audio))
        self.video_mode = True
        self.audio_mode = is_audio
        if self.events is not None:
            self.events.append(("proj_bar", "activate_video", title, is_audio))

    def activate_image(
        self,
        title,
        image_data=None,
        keep_expanded=False,
        initial_transform=None,
        **options,
    ):
        self.images.append((title, image_data, keep_expanded))
        self.initial_transforms.append(initial_transform)
        self.image_activation_options.append(options)
        self.video_mode = False
        self.audio_mode = False

    def set_projected_image_transform(self, transform):
        self.projected_image_transforms.append(transform)
        return transform

    def is_expanded(self):
        return self.expanded

    def is_video_mode(self):
        return self.video_mode

    def is_audio_mode(self):
        return self.audio_mode

    def set_projected_title(self, title):
        self.projected_titles.append(title)

    def hide_add_to_destination_action(self):
        self.hidden_add_to_destination += 1

    def set_live_tab_mode(self, enabled):
        self.live_tab_modes.append(enabled)

    def update_tab_live_preview(self, frame):
        self.tab_previews.append(frame)

class _AutoKeyProjectionStub:
    def __init__(self, events=None):
        self.prepared = 0
        self.events = events

    def prepare_video_session(self):
        self.prepared += 1
        if self.events is not None:
            self.events.append(("auto_key", "prepare_video_session"))


class _ProjectionIntegrationsStub:
    def __init__(self, playback):
        self.playback = playback
        self.statuses = []
        self.wait_for_auto_share = False

    def update_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))

    def prepare_video_playback_for_auto_share(self, resume_on_success):
        if self.wait_for_auto_share:
            self.playback.begin_auto_share_wait(resume_on_success=resume_on_success)
        return self.wait_for_auto_share


class _ProjectionWindowStub:
    def __init__(self, events=None):
        self.events = events
        self.native_output_active = False
        self.cleared = 0
        self.began_video = 0
        self.images = []
        self.pixmaps = []
        self.frames = []
        self.transforms = []
        self.image_initial_transforms = []
        self.instant_resets = 0

    def clear(self):
        self.cleared += 1
        if self.events is not None:
            self.events.append(("projection_window", "clear"))

    def begin_video(self):
        self.began_video += 1
        if self.events is not None:
            self.events.append(("projection_window", "begin_video"))

    def show_image_from_url_data(self, data, *, initial_transform=None):
        self.images.append(data)
        self.image_initial_transforms.append(initial_transform)

    def show_image_from_pixmap(self, frame):
        self.pixmaps.append(frame)

    def update_frame(self, frame):
        self.frames.append(frame)

    def set_image_transform(self, zoom, norm_x, norm_y, *, animate=True):
        self.transforms.append((zoom, norm_x, norm_y, animate))

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

    def resolve_media(self, item):
        return self.resolved


class _WindowStub:
    def __init__(self):
        self.events = []
        self.projection_session = ProjectionSession()
        self.projection_session.set_tab_projection_active(True)
        self._navigation = _NavigationStub()
        self.media_ctrl = _ServiceStub(self.events, "media")
        self._ndi_service = _ServiceStub(self.events, "ndi")
        self.proj_bar = _ProjectionBarStub(self.events)
        self.settings_widget = _SettingsStub()
        self.playback_protection = _ProtectionStub()
        self.playback = ProjectionPlaybackOperations(
            self.media_ctrl,
            SimpleNamespace(
                volume=lambda: 0.8,
                speed=lambda: 1.0,
                playback_order=lambda: ORDER_OFF,
                loop_enabled=lambda: False,
                set_loop_enabled=lambda _enabled: None,
            ),
            self.playback_protection,
        )
        self.auto_stops = 0
        self._auto_key_projection = _AutoKeyProjectionStub(self.events)
        self._projection_integrations = _ProjectionIntegrationsStub(self.playback)
        self.playlist_widget = _PlaylistWidgetStub()
        self.meeting_service = _MeetingServiceStub()
        self.windows = [
            _ProjectionWindowStub(self.events),
            _ProjectionWindowStub(self.events),
        ]
        self.content_frames = []

    def stop_projection(self):
        self.auto_stops += 1

    def _all_windows(self):
        return self.windows

    def tr(self, text):
        return text


def _controller(window, *, content_frame_sink=None, image_source_lease=nullcontext):
    return MediaProjectionController(
        MediaProjectionContext(
            projection_session=window.projection_session,
            projection_bar=window.proj_bar,
            media_controller=window.media_ctrl,
            ndi_service=window._ndi_service,
            projection_windows=window._all_windows,
            playlist_edit_is_temp=lambda: getattr(
                window.playlist_widget._edit_view,
                "_is_temp",
                False,
            ),
            meeting_service=lambda: window.meeting_service,
            dialog_parent=window,
            sjjm_announce_mode=window.settings_widget.get_sjjm_announce_mode,
            start_videos_paused=window.settings_widget.get_start_videos_paused,
            projection_playback=window.playback,
            content_frame_sink=content_frame_sink or window.content_frames.append,
            image_source_lease=image_source_lease,
        ),
        MediaProjectionHandlers(
            stop_projection=window.stop_projection,
            stop_browser_tab_projection=(window._navigation.stop_browser_tab_projection),
            update_projection_status=(window._projection_integrations.update_status),
            prepare_video_session=(window._auto_key_projection.prepare_video_session),
            prepare_auto_share_playback=(
                window._projection_integrations.prepare_video_playback_for_auto_share
            ),
        ),
    )


def _png_bytes(width=64, height=36):
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#334466"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(payload)


def _write_png(path):
    path.write_bytes(_png_bytes())


def test_image_identity_is_committed_before_first_frame():
    window = _WindowStub()
    events = []
    window.projection_session.subscribe(
        lambda: events.append(("state", window.projection_session.session_id))
    )
    controller = _controller(
        window,
        content_frame_sink=lambda _frame: events.append(
            ("frame", window.projection_session.session_id)
        ),
    )

    controller.project_image_bytes(_png_bytes())

    assert events[-2:] == [("state", 1), ("frame", 1)]


def test_image_file_is_leased_until_projection_ownership_is_committed(tmp_path):
    from solin.core.media.publication_assets import PublicationAssetStore

    store = PublicationAssetStore(tmp_path / "assets")
    source = tmp_path / "source.png"
    _write_png(source)
    path = store.materialize(source)
    window = _WindowStub()
    collected = []

    def commit_owner():
        collected.extend(store.collect(set))
        store.retain("projection", [window.projection_session.state.get("source_path", "")])

    unsubscribe = window.projection_session.subscribe(commit_owner)
    controller = _controller(window, image_source_lease=lambda path: store.lease([path]))
    try:
        controller._project_image_path("Slide", path)
        assert not collected
        assert window.projection_session.state["source_path"] == path
        assert store.collect(set) == []
        with pytest.raises(OSError):
            store.remove(path)
    finally:
        unsubscribe()
        store.release("projection")
    assert store.collect(set) == [path]


def test_reframing_identical_bytes_transfers_the_projection_source_pin(tmp_path):
    from solin.core.media.publication_assets import PublicationAssetStore

    store = PublicationAssetStore(tmp_path / "assets")
    source = tmp_path / "source.png"
    _write_png(source)
    path = store.materialize(source)
    window = _WindowStub()
    unsubscribe = window.projection_session.subscribe(lambda: store.retain(
        "projection", [window.projection_session.state.get("source_path", "")],
    ))
    controller = _controller(window, image_source_lease=lambda path: store.lease([path]))
    try:
        controller._project_image_path("Original", str(source))
        controller._project_image_path("Shared", path, image_framing={
            "version": 1, "zoom": 2.0, "norm_x": 0.0, "norm_y": 0.0,
        })

        assert window.projection_session.state["source_path"] == path
        assert store.collect(set) == []
    finally:
        unsubscribe()
        store.release("projection")


def test_video_identity_is_committed_before_decoder_can_emit():
    window = _WindowStub()
    events = []
    window.projection_session.subscribe(
        lambda: events.append(("state", window.projection_session.session_id))
    )
    window.media_ctrl.start_playback = lambda _request: events.append(
        ("decoder", window.projection_session.session_id)
    )
    controller = _controller(window)

    assert controller.project_video("clip.mp4", "Clip")

    assert events[-2:] == [("state", 1), ("decoder", 1)]


def test_generated_theme_image_uses_non_persistent_image_pipeline() -> None:
    window = _WindowStub()
    controller = _controller(window)
    data = _png_bytes(192, 108)

    result = controller.project_generated_image(
        "Talk theme",
        data,
        {"generated_kind": "talk_theme", "fingerprint": "scene-123"},
    )

    assert result.status is ProjectionResultStatus.ACCEPTED
    assert window.projection_session.state == {
        "type": "image",
        "title": "Talk theme",
        "origin": None,
        "data": data,
        "transform": (1.0, 0.0, 0.0),
        "generated_kind": "talk_theme",
        "fingerprint": "scene-123",
    }
    assert window.proj_bar.image_activation_options == [
        {
            "persist_operator_copy": False,
            "allow_add_to_destination": False,
            "allow_set_as_idle": False,
        }
    ]
    assert [projection_window.images for projection_window in window.windows] == [
        [data],
        [data],
    ]
    assert window._projection_integrations.statuses[-1][1] == {"auto_keys_media": False}


def test_invalid_generated_image_has_no_projection_side_effects() -> None:
    window = _WindowStub()
    controller = _controller(window)

    result = controller.project_generated_image("Broken", b"not-an-image")

    assert result.status is ProjectionResultStatus.INVALID
    assert window.events == []
    assert window.proj_bar.images == []
    assert all(projection_window.images == [] for projection_window in window.windows)


def test_generated_image_checks_playback_protection_at_commit() -> None:
    window = _WindowStub()
    window.playback_protection.locked = True
    controller = _controller(window)

    result = controller.project_generated_image("Protected", _png_bytes())

    assert result.status is ProjectionResultStatus.BLOCKED
    assert window.playback_protection.blocked == 1
    assert window.events == []


def test_project_video_classifies_audio_and_updates_status():
    window = _WindowStub()
    controller = _controller(window)

    controller.project_video("song.mp3", "Song")

    assert window.playback.queue.items == ({"url": "song.mp3", "title": "Song", "type": "audio"},)
    assert window.proj_bar.videos == [("Song", False, True)]
    assert [projection_window.began_video for projection_window in window.windows] == [0, 0]
    assert window.media_ctrl.played == ["song.mp3"]
    assert window.projection_session.state == {
        "type": "video",
        "is_audio": True,
        "title": "Song",
        "origin": None,
    }
    assert window._projection_integrations.statuses == [
        ((True, "Song"), {"visual": False, "auto_keys_media": False})
    ]


def test_project_video_snapshots_custom_times_into_playback_request():
    window = _WindowStub()
    controller = _controller(window)
    item = {
        "url": "talk.mp4",
        "title": "Talk",
        "type": "video",
        "start_trim_ticks": 20_000_000,
        "end_trim_ticks": 30_000_000,
        "base_duration_ticks": 100_000_000,
        "origin_container_id": "saved-playlist",
        "origin_item_id": "trimmed-occurrence",
    }

    controller.project_video("talk.mp4", "Talk", [item])

    request = window.media_ctrl.requests[-1]
    assert request.source == "talk.mp4"
    assert request.trim is not None
    assert request.trim.start_trim_ticks == 20_000_000
    assert request.trim.end_trim_ticks == 30_000_000
    assert request.occurrence_container_id == "saved-playlist"
    assert request.occurrence_id == "trimmed-occurrence"


def test_resolved_meeting_video_preserves_catalog_origin_in_projection_queue():
    window = _WindowStub()
    window.meeting_service.resolved = {
        "url": "https://media.example.test/song.mp4",
        "title": "34. Andarei em integridade",
    }
    controller = _controller(window)
    item = MeetingMedia(
        mime_type="video/mp4",
        label="34. Andarei em integridade",
        key_symbol="sjjm",
        track=34,
        origin_kind="meeting",
        origin_container_id="mwb:2026-07-13:T:20260700",
        origin_item_id="song-34",
    )

    controller.on_meeting_media_project(item)

    projected = window.playback.queue.current_item
    assert projected["origin_kind"] == "meeting"
    assert projected["origin_container_id"] == "mwb:2026-07-13:T:20260700"
    assert projected["origin_item_id"] == "song-34"
    assert window.projection_session.state["origin"] == {
        "kind": "meeting",
        "container_id": "mwb:2026-07-13:T:20260700",
        "item_id": "song-34",
    }


def test_manual_projection_is_rejected_before_any_state_changes_when_locked():
    window = _WindowStub()
    window.playback_protection.locked = True
    controller = _controller(window)

    result = controller.project_video("next.mp4", "Next")

    assert result is False
    assert window.playback_protection.blocked == 1
    assert not window.playback.queue.items
    assert window.proj_bar.videos == []
    assert window.media_ctrl.stopped == 0
    assert window.media_ctrl.played == []
    assert all(projection_window.cleared == 0 for projection_window in window.windows)


def test_automatic_advance_bypasses_manual_playback_protection():
    window = _WindowStub()
    controller = _controller(window)
    window.playback.commit_queue(window.playback.new_queue([
        {"url": "current.mp4", "title": "Current"},
        {"url": "automatic.mp4", "title": "Automatic"},
    ], ORDER_NEXT))
    window.projection_session.set_state({"type": "video", "title": "Current"})
    window.playback_protection.locked = True

    controller.on_media_ended()

    assert window.playback_protection.blocked == 0
    assert window.media_ctrl.played == ["automatic.mp4"]
    assert window.proj_bar.videos == [("Automatic", True, False)]


def test_project_video_uses_announcement_mode_for_sjjm_video():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller.project_video("video.mp4", "Video", keep_expanded=True, announce_song=True)

    assert window.proj_bar.videos == [("Video", True, False)]
    assert window.events.count(("media", "audio_suppressed", True)) == 1
    assert window._auto_key_projection.prepared == 1
    assert window.media_ctrl.played == ["video.mp4"]
    assert window.media_ctrl.paused == 0
    assert [projection_window.began_video for projection_window in window.windows] == [1, 1]
    assert window.events.index(("media", "audio_suppressed", True)) < (
        window.events.index(("media", "start_playback", "video.mp4"))
    )
    assert ("media", "pause") not in window.events


def test_custom_times_bypass_song_announcement_mode():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    controller = _controller(window)
    item = {
        "url": "song.mp4",
        "type": "video",
        "start_trim_ticks": 10_000_000,
        "end_trim_ticks": 20_000_000,
        "base_duration_ticks": 100_000_000,
    }

    controller.project_video("song.mp4", "Song", [item], announce_song=True)

    assert ("media", "audio_suppressed", True) not in window.events
    assert window.media_ctrl.requests[-1].trim is not None
    assert window.media_ctrl.requests[-1].autoplay is True


def test_project_video_starts_regular_visual_videos_paused_when_enabled():
    window = _WindowStub()
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)

    controller.project_video("talk.mp4", "Talk")

    assert ("media", "audio_suppressed", True) not in window.events
    assert window.media_ctrl.played == ["talk.mp4"]
    assert window.media_ctrl.requests[-1].autoplay is False
    assert window.media_ctrl.paused == 0
    assert window._auto_key_projection.prepared == 1
    assert [projection_window.began_video for projection_window in window.windows] == [1, 1]


def test_project_video_starts_visual_video_paused_while_auto_share_is_pending():
    window = _WindowStub()
    window._projection_integrations.wait_for_auto_share = True
    controller = _controller(window)

    controller.project_video("talk.mp4", "Talk")

    assert window.media_ctrl.requests[-1].autoplay is False
    window.playback.resolve_auto_share_wait(True)
    assert window.media_ctrl.resumed == 1


def test_auto_share_success_respects_the_profile_pause_for_a_regular_video():
    window = _WindowStub()
    window.settings_widget.start_videos_paused = True
    window._projection_integrations.wait_for_auto_share = True
    controller = _controller(window)

    assert controller.project_video("talk.mp4", "Talk")
    assert not window.media_ctrl.requests[-1].autoplay
    window.playback.resolve_auto_share_wait(True)

    assert window.media_ctrl.resumed == 0
    assert window.playback.resume()
    assert window.media_ctrl.resumed == 1


def test_auto_share_pause_does_not_override_song_announcement_mode():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    window._projection_integrations.wait_for_auto_share = True
    controller = _controller(window)
    controller.project_video("song.mp4", "Song", announce_song=True)

    assert window.events.count(("media", "audio_suppressed", True)) == 1
    assert window.media_ctrl.requests[-1].autoplay is False
    window.playback.resolve_auto_share_wait(True)
    assert window.media_ctrl.resumed == 1
    assert window.playback.transport_blocked


@pytest.mark.parametrize("share_success", [None, False, True])
def test_explicitly_paused_announcement_starts_only_on_operator_request(share_success):
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window._projection_integrations.wait_for_auto_share = share_success is not None
    controller = _controller(window)
    song = MeetingMedia(key_symbol="sjjm", track=1, file_path="song.mp4", mime_type="video/mp4")

    assert controller.on_meeting_media_project(song, start_paused=True).accepted
    assert not window.media_ctrl.requests[-1].autoplay
    if share_success is not None:
        window.playback.resolve_auto_share_wait(share_success)
    assert window.media_ctrl.resumed == 0

    assert window.playback.resume()
    assert window.media_ctrl.resumed == 1
    assert window.playback.transport_blocked
    window.media_ctrl.position_changed.emit(3500)
    assert window.media_ctrl.paused == 1
    assert window.playback.resume()
    assert ("media", "audio_suppressed", False) in window.events


@pytest.mark.parametrize(("duration_ms", "announced"), [(5000, False), (5001, True)])
def test_known_song_duration_determines_announcement_and_profile_priority(duration_ms, announced):
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    song = {"url": "song.mp4", "base_duration_ticks": duration_ms * 10_000}

    assert controller.project_video("song.mp4", "Song", [song], announce_song=True)

    assert window.media_ctrl.requests[-1].autoplay is announced
    assert (("media", "audio_suppressed", True) in window.events) is announced


@pytest.mark.parametrize("preference_paused", [False, True])
def test_decoder_duration_bypasses_announcement_for_an_unprobed_short_song(preference_paused):
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = preference_paused
    controller = _controller(window)
    assert controller.project_video("song.mp4", "Song", announce_song=True)

    window.media_ctrl.position = 100
    window.media_ctrl.duration_changed.emit(2000)

    assert not window.playback.transport_blocked
    assert window.events[-1] == ("media", "audio_suppressed", False)
    if preference_paused:
        assert window.media_ctrl.paused == 1
        assert window.media_ctrl.position == 0
        assert window.media_ctrl.replayed == 0
    else:
        assert window.media_ctrl.paused == 0
        assert window.media_ctrl.replayed == 1


@pytest.mark.parametrize("loop", [False, True])
@pytest.mark.parametrize("duration_reported_at_end", [False, True])
def test_unprobed_song_ending_during_announcement_can_restart_with_audio(
    loop, duration_reported_at_end
):
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    controller = _controller(window)
    if loop:
        window.playback.toggle_loop()
    assert controller.project_video("song.mp4", "Song", announce_song=True)

    window.media_ctrl.is_ended = True
    if duration_reported_at_end:
        # The first decoder sample with a duration can already be ended at position zero.
        window.media_ctrl.duration_changed.emit(2000)
    controller.on_media_ended()

    assert window.auto_stops == 0
    assert window.media_ctrl.replayed == 0
    assert window.playback.resume()
    assert ("media", "audio_suppressed", False) in window.events
    assert window.media_ctrl.replayed == 1

    controller.on_media_ended()

    assert window.auto_stops == int(not loop)
    assert window.media_ctrl.replayed == 1 + int(loop)


def test_project_video_pauses_sjjm_video_when_announcement_mode_is_disabled():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = False
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller.project_video("song.mp4", "Song", announce_song=True)

    assert ("media", "audio_suppressed", True) not in window.events
    assert window.media_ctrl.played == ["song.mp4"]
    assert window.media_ctrl.requests[-1].autoplay is False


def test_project_video_ignores_announcement_for_non_sjjm_videos_but_still_pauses():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)

    controller.project_video("regular.mp4", "Regular")

    assert ("media", "audio_suppressed", True) not in window.events
    assert window.media_ctrl.played == ["regular.mp4"]
    assert window.media_ctrl.requests[-1].autoplay is False


def test_project_video_never_start_pauses_audio_or_enters_song_announcement():
    window = _WindowStub()
    window.settings_widget.sjjm_announce_mode = True
    window.settings_widget.start_videos_paused = True
    controller = _controller(window)
    controller.project_video("song.mp3", "Song Audio", announce_song=True)

    assert window.proj_bar.videos == [("Song Audio", False, True)]
    assert ("media", "audio_suppressed", True) not in window.events
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

    assert window.playback.queue.items == tuple(playlist)
    assert window.playback.queue.from_saved_playlist
    assert window.events.count(("media", "audio_suppressed", True)) == 1
    assert window.media_ctrl.played == ["song.mp4"]
    assert window.media_ctrl.paused == 0

    controller.project_video("talk.mp4", "Talk")

    assert window.events.count(("media", "audio_suppressed", True)) == 1
    assert window.media_ctrl.played == ["song.mp4", "talk.mp4"]


def test_on_playlist_project_image_keeps_playlist_and_saved_source(tmp_path):
    window = _WindowStub()
    window.proj_bar.expanded = True
    controller = _controller(window)
    image_path = tmp_path / "slide.png"
    _write_png(image_path)
    playlist = [{"url": str(image_path), "title": "Slide", "type": "image"}]

    controller.on_playlist_project(str(image_path), "Slide", playlist, "random")

    assert window.playback.queue.items == tuple(playlist)
    assert window.playback.queue.order == ORDER_RANDOM
    assert window.playback.queue.from_saved_playlist
    assert window.proj_bar.images == [("Slide", image_path.read_bytes(), True)]
    assert [projection_window.images for projection_window in window.windows] == [
        [image_path.read_bytes()],
        [image_path.read_bytes()],
    ]
    assert window.projection_session.state == {
        "type": "image",
        "title": "Slide",
        "origin": None,
        "data": image_path.read_bytes(),
        "transform": (1.0, 0.0, 0.0),
        "source_path": str(image_path),
    }


def test_prepared_image_framing_is_applied_instantly_and_saved_in_session(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "prepared.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))
    framing = {
        "version": 1,
        "zoom": 1.5,
        "norm_x": 0.2,
        "norm_y": 0.0,
    }
    playlist = [
        {
            "url": str(image_path),
            "title": "Prepared",
            "type": "image",
            "image_framing": framing,
        }
    ]

    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    expected = ImageTransform(1.5, 0.2, 0.0)
    assert window.projection_session.state["transform"] == (1.5, 0.2, 0.0)
    assert window.proj_bar.initial_transforms[-1] == expected
    assert [surface.image_initial_transforms[-1] for surface in window.windows] == [
        expected,
        expected,
    ]
    assert all(surface.transforms == [] for surface in window.windows)


def test_reprojecting_same_image_animates_only_changed_prepared_framing(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "prepared.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))
    playlist = [
        {
            "url": str(image_path),
            "title": "Prepared",
            "type": "image",
            "image_framing": {
                "version": 1,
                "zoom": 1.5,
                "norm_x": 0.2,
                "norm_y": 0.0,
            },
        }
    ]
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    playlist[0]["image_framing"] = {
        "version": 1,
        "zoom": 2.0,
        "norm_x": -0.25,
        "norm_y": 0.1,
    }
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    expected = ImageTransform(2.0, -0.25, 0.1)
    assert window.projection_session.state["transform"] == (2.0, -0.25, 0.1)
    assert window.proj_bar.projected_image_transforms == [expected]
    assert len(window.proj_bar.images) == 1
    assert window.playback.queue.items == tuple(playlist)
    assert window._navigation.stopped == 1
    assert window.media_ctrl.stopped == 1
    assert window._ndi_service.stopped == 1
    assert len(window._projection_integrations.statuses) == 1
    for surface in window.windows:
        assert surface.cleared == 1
        assert len(surface.images) == 1
        assert surface.transforms == [(2.0, -0.25, 0.1, True)]


def test_reprojecting_same_image_with_same_framing_reloads_normally(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "prepared.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))
    framing = {
        "version": 1,
        "zoom": 1.5,
        "norm_x": 0.2,
        "norm_y": 0.0,
    }
    playlist = [
        {
            "url": str(image_path),
            "title": "Prepared",
            "type": "image",
            "image_framing": framing,
        }
    ]

    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    assert window.proj_bar.projected_image_transforms == []
    assert len(window.proj_bar.images) == 2
    assert window.playback.queue.items == tuple(playlist)
    assert window._navigation.stopped == 2
    assert len(window._projection_integrations.statuses) == 2
    for surface in window.windows:
        assert surface.cleared == 2
        assert len(surface.images) == 2
        assert surface.transforms == []


def test_unchanged_framing_skips_image_content_comparison():
    window = _WindowStub()
    controller = _controller(window)
    transform = ImageTransform(1.5, 0.2, 0.0)
    window.projection_session.set_state(
        {
            "type": "image",
            "data": _UncomparableBytes(b"active-image"),
            "transform": (transform.zoom, transform.norm_x, transform.norm_y),
        }
    )

    assert not controller._update_active_image_framing(
        b"different-image",
        transform,
    )


def test_reprojecting_changed_image_content_reloads_normally(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "prepared.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))
    playlist = [
        {
            "url": str(image_path),
            "title": "Prepared",
            "type": "image",
            "image_framing": {
                "version": 1,
                "zoom": 1.5,
                "norm_x": 0.2,
                "norm_y": 0.0,
            },
        }
    ]
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    image.fill(QColor("#000000"))
    assert image.save(str(image_path))
    playlist[0]["image_framing"] = {
        "version": 1,
        "zoom": 2.0,
        "norm_x": -0.25,
        "norm_y": 0.1,
    }
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    assert window.proj_bar.projected_image_transforms == []
    assert len(window.proj_bar.images) == 2
    for surface in window.windows:
        assert surface.cleared == 2
        assert len(surface.images) == 2
        assert surface.transforms == []


def test_reprojecting_same_image_without_framing_animates_back_to_identity(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "prepared.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))
    playlist = [
        {
            "url": str(image_path),
            "title": "Prepared",
            "type": "image",
            "image_framing": {
                "version": 1,
                "zoom": 1.5,
                "norm_x": 0.2,
                "norm_y": 0.0,
            },
        }
    ]
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    playlist[0].pop("image_framing")
    controller.on_playlist_project(str(image_path), "Prepared", playlist, "")

    expected = ImageTransform(1.0, 0.0, 0.0)
    assert window.projection_session.state["transform"] == (1.0, 0.0, 0.0)
    assert window.proj_bar.projected_image_transforms == [expected]
    assert len(window.proj_bar.images) == 1
    for surface in window.windows:
        assert surface.cleared == 1
        assert len(surface.images) == 1
        assert surface.transforms == [(1.0, 0.0, 0.0, True)]


def test_automatic_replay_keeps_the_current_decoder_and_presentation():
    window = _WindowStub()
    controller = _controller(window)
    window.projection_session.set_state({"type": "video", "title": "Current"})
    epoch = window.projection_session.presentation_session_id

    window.playback.toggle_loop()
    controller.on_media_ended()

    assert window.media_ctrl.replayed == 1
    assert window.media_ctrl.stopped == 0
    assert window.media_ctrl.requests == []
    assert window.projection_session.presentation_session_id == epoch


def test_automatic_advance_keeps_complete_image_item_framing(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "next.png"
    image = QImage(1600, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    assert image.save(str(image_path))

    controller.project_media_at_index(
        [
            {"url": "current.mp4", "title": "Current"},
            {
                "url": str(image_path),
                "title": "Next",
                "type": "image",
                "image_framing": {
                    "version": 1,
                    "zoom": 1.5,
                    "norm_x": -0.2,
                    "norm_y": 0.0,
                },
            },
        ],
        playback_order=ORDER_NEXT,
    )
    controller.on_media_ended()

    assert window.playback.queue.index == 1
    assert window.projection_session.state["transform"] == (1.5, -0.2, 0.0)


def test_project_tab_frame_initializes_live_tab_once():
    window = _WindowStub()
    window.projection_session.set_tab_projection_active(False)
    controller = _controller(window)
    frame = object()

    controller.project_tab_frame(frame)
    controller.project_tab_frame(frame)

    assert window.projection_session.tab_projection_active is True
    assert not window.playback.queue.items
    assert window.proj_bar.images[0][0] == "Browser — Live Tab"
    assert window.proj_bar.hidden_add_to_destination == 1
    assert window.proj_bar.live_tab_modes == [True]
    assert window.proj_bar.tab_previews == [frame, frame]
    assert [projection_window.pixmaps for projection_window in window.windows] == [
        [frame, frame],
        [frame, frame],
    ]
    assert window.content_frames == [frame, frame]


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


def test_instant_image_transform_is_persisted_without_animation():
    window = _WindowStub()
    controller = _controller(window)
    window.projection_session.set_state(
        {"type": "image", "data": b"x", "transform": (1.0, 0.0, 0.0)}
    )

    controller.on_image_apply_transform_instant(1.8, 0.0, 0.2)

    assert window.projection_session.state["transform"] == (1.8, 0.0, 0.2)
    assert [projection_window.transforms for projection_window in window.windows] == [
        [(1.8, 0.0, 0.2, False)],
        [(1.8, 0.0, 0.2, False)],
    ]


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
    window.projection_session.set_state({"type": "video", "title": "Pending"})
    controller.on_title_from_metadata("Resolved")

    assert window.proj_bar.projected_titles == ["Resolved"]
    assert window.projection_session.state["title"] == "Resolved"


def test_project_media_at_index_sets_playlist_index_for_images(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image_path = tmp_path / "second.png"
    _write_png(image_path)
    playlist = [
        {"url": "video.mp4", "title": "Video", "type": "video"},
        {"url": str(image_path), "title": "Second", "type": "image"},
    ]

    controller.project_media_at_index(playlist, index=1, keep_expanded=True)

    assert window.playback.queue.items == tuple(playlist)
    assert window.playback.queue.index == 1
    assert window.proj_bar.images == [("Second", image_path.read_bytes(), True)]


def test_media_projection_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")


def test_unreadable_queue_image_does_not_change_selection_or_projection(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    playlist = [
        {"url": "current.mp4", "title": "Current"},
        {"url": str(tmp_path / "missing.png"), "title": "Next", "type": "image"},
    ]
    result = controller.project_media_at_index(playlist, playback_order=ORDER_NEXT)
    assert result.accepted
    session_id = window.projection_session.session_id

    assert controller.on_playlist_navigate(1).status is ProjectionResultStatus.FAILED
    assert controller.on_playlist_navigate(-1).status is ProjectionResultStatus.INVALID
    assert window.playback.queue.index == 0
    assert window.playback.queue.played == {0}
    assert window.projection_session.session_id == session_id
    assert not window.proj_bar.images

    controller.on_media_ended()

    assert window.auto_stops == 1


def test_queue_navigation_keeps_occurrences_separate_when_source_is_reused():
    window = _WindowStub()
    controller = _controller(window)
    playlist = [
        {"url": "same.mp4", "title": "First", "id": "first", "start_trim_ticks": 1_000_000},
        {"url": "same.mp4", "title": "Second", "id": "second", "start_trim_ticks": 2_000_000},
    ]

    assert controller.project_media_at_index(playlist, index=1).accepted
    assert controller.on_playlist_navigate(0).accepted

    first_open, second_open = window.media_ctrl.requests
    assert (first_open.occurrence_id, first_open.trim.start_trim_ticks) == ("second", 2_000_000)
    assert (second_open.occurrence_id, second_open.trim.start_trim_ticks) == ("first", 1_000_000)
    assert window.playback.queue.current_item["id"] == "first"


def test_blank_video_source_leaves_the_active_projection_alone():
    window = _WindowStub()
    controller = _controller(window)
    assert controller.project_media_at_index([
        {"url": "active.mp4", "title": "Active"},
        {"url": "   ", "title": "Unavailable"},
    ]).accepted
    session_id = window.projection_session.session_id
    queue = window.playback.queue
    stops = window.media_ctrl.stopped

    assert controller.on_playlist_navigate(1).status is ProjectionResultStatus.INVALID
    assert not controller.project_video(" \t", "Unavailable")
    assert window.playback.queue is queue
    assert window.projection_session.session_id == session_id
    assert window.media_ctrl.stopped == stops
    assert window.media_ctrl.played == ["active.mp4"]


def test_explicit_paused_playback_waits_for_operator_after_auto_share():
    window = _WindowStub()
    window._projection_integrations.wait_for_auto_share = True
    controller = _controller(window)

    result = controller.project_media_at_index(
        [{"url": "clip.mp4", "title": "Clip"}], start_paused=True
    )
    assert result.accepted
    assert not window.media_ctrl.requests[-1].autoplay

    window.playback.resolve_auto_share_wait(True)

    assert window.media_ctrl.resumed == 0
    assert window.playback.resume()
    assert window.media_ctrl.resumed == 1


def test_unresolvable_meeting_media_leaves_previous_playback_alone():
    window = _WindowStub()
    controller = _controller(window)
    assert controller.project_video("clip.mp4", "Clip")
    session_id = window.projection_session.session_id

    result = controller.on_meeting_media_project(
        MeetingMedia(key_symbol="sjjm", track=1), notify=False, start_paused=True
    )

    assert result.status is ProjectionResultStatus.FAILED
    assert window.projection_session.session_id == session_id
    assert window.media_ctrl.paused == 0
    assert window.media_ctrl.played == ["clip.mp4"]


def test_queue_selection_reidentifies_identical_image_bytes(tmp_path):
    window = _WindowStub()
    controller = _controller(window)
    image = tmp_path / "slide.png"
    image.write_bytes(_png_bytes(1600, 900))
    playlist = [
        {"url": str(image), "title": "First", "type": "image"},
        {
            "url": str(image),
            "title": "Second",
            "type": "image",
            "image_framing": {"version": 1, "zoom": 1.5, "norm_x": 0.1, "norm_y": 0},
        },
    ]
    assert controller.project_media_at_index(playlist).accepted
    before = window.projection_session.session_id

    assert controller.on_playlist_navigate(1).accepted

    assert window.projection_session.session_id != before
    assert window.projection_session.state["title"] == "Second"
    assert window.playback.queue.index == 1


def test_synchronous_transport_failure_does_not_publish_active_projection():
    window = _WindowStub()
    controller = _controller(window)
    start_playback = window.media_ctrl.start_playback

    def fail_on_open(request):
        start_playback(request)
        window.playback.reset_projection()
        window.projection_session.reset_state()

    window.media_ctrl.start_playback = fail_on_open

    result = controller.project_media_at_index([{"url": "clip.mp4", "title": "Clip"}])

    assert result.status is ProjectionResultStatus.FAILED
    assert not window.playback.queue.items
    assert window.projection_session.state_type == "idle"
    assert window._projection_integrations.statuses == []


def test_manual_queue_navigation_requires_playback_protection_to_be_unlocked():
    window = _WindowStub()
    controller = _controller(window)
    playlist = [
        {"url": "first.mp4", "title": "First"},
        {"url": "second.mp4", "title": "Second"},
    ]
    assert controller.project_media_at_index(playlist).accepted
    window.playback_protection.locked = True

    assert not window.playback.can_navigate(1)
    assert controller.on_playlist_navigate(0).accepted
    assert controller.on_playlist_navigate(1).status is ProjectionResultStatus.BLOCKED
    assert window.playback_protection.blocked == 1
    assert window.playback.queue.index == 0
    assert window.media_ctrl.played == ["first.mp4"]

    window.playback_protection.locked = False
    assert window.playback.can_navigate(1)
    assert controller.on_playlist_navigate(1).accepted
    assert window.playback.queue.index == 1
    assert window.media_ctrl.played == ["first.mp4", "second.mp4"]
