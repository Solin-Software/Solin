from types import SimpleNamespace

from solin.controllers.wifi_playlist_controller import (
    WifiPlaylistContext,
    WifiPlaylistController,
    WifiPlaylistHandlers,
)
from solin.widgets.wifi_receive_widget import WifiReceiveWidget


class _MediaProjectionStub:
    def __init__(self):
        self.cache_calls = []
        self.video_calls = []

    def on_cache_play(self, path, media_type, original_url="", display_title=""):
        self.cache_calls.append((path, media_type, original_url, display_title))

    def project_video(
        self,
        path,
        title,
        playlist,
        playback_order,
        from_saved_playlist=False,
    ):
        self.video_calls.append(
            (path, title, playlist, playback_order, from_saved_playlist)
        )


class _PlaylistWidgetStub:
    def __init__(self):
        self.created = []
        self.added = []
        self.add_results = []

    def create_playlist_with_item(self, name, item):
        self.created.append((name, item))
        return "playlist-created"

    def add_item_to_playlist(self, playlist_id, item):
        self.added.append((playlist_id, item))
        return self.add_results.pop(0) if self.add_results else True


class _WifiReceiveWidgetStub:
    def __init__(self):
        self._received_files = []
        self._wifi_tmp_files = set()
        self.removed = []
        self.clear_all_count = 0

    def remove_received_file(self, path):
        self.removed.append(path)

    def clear_all_received(self):
        self.clear_all_count += 1

    def received_entry(self, path):
        return next(
            (dict(entry) for entry in self._received_files if entry.get("path") == path),
            {},
        )

    def preserve_temp_file(self, path):
        self._wifi_tmp_files.discard(path)


class _NotificationsStub:
    def __init__(self):
        self.successes = []

    def success(self, message):
        self.successes.append(message)


class _WindowStub:
    def __init__(self):
        self._media_projection = _MediaProjectionStub()
        self.playlist_widget = _PlaylistWidgetStub()
        self.wifi_receive_widget = _WifiReceiveWidgetStub()
        self.notifications = _NotificationsStub()

    def tr(self, text, *_args):
        return text


def _controller(window):
    return WifiPlaylistController(
        WifiPlaylistContext(
            dialog_parent=window,
            playlist_widget=window.playlist_widget,
            notifications=window.notifications,
            translate=window.tr,
            wifi_receive_widget=lambda: window.wifi_receive_widget,
        ),
        WifiPlaylistHandlers(
            play_cached_media=window._media_projection.on_cache_play,
            project_video=window._media_projection.project_video,
        ),
    )


def test_on_wifi_request_play_routes_images_audio_and_video():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_wifi_request_play("slide.png", "Slide")
    controller.on_wifi_request_play("song.mp3", "Song")
    controller.on_wifi_request_play("clip.mp4", "Clip")

    assert window._media_projection.cache_calls == [
        ("slide.png", "image", "", "Slide")
    ]
    assert window._media_projection.video_calls == [
        (
            "song.mp3",
            "Song",
            [{"url": "song.mp3", "title": "Song", "type": "audio"}],
            None,
            False,
        ),
        (
            "clip.mp4",
            "Clip",
            [{"url": "clip.mp4", "title": "Clip", "type": "video"}],
            None,
            False,
        ),
    ]


def test_item_from_wifi_entry_preserves_jw_metadata_and_original_filename():
    window = _WindowStub()
    controller = _controller(window)

    item = controller.item_from_wifi_entry({
        "path": "song.mp3",
        "title": "Song",
        "type": "audio",
        "key_symbol": "sjjm",
        "track": 12,
        "issue_tag": None,
        "doc_id": "abc",
        "meps_language": 3,
        "orig_name": "original.mp3",
    })

    assert item["title"] == "Song"
    assert item["url"] == "song.mp3"
    assert item["type"] == "audio"
    assert item["key_symbol"] == "sjjm"
    assert item["track"] == 12
    assert item["doc_id"] == "abc"
    assert item["meps_language"] == 3
    assert item["original_filename"] == "original.mp3"
    assert "issue_tag" not in item or item["issue_tag"] is None


def test_add_single_to_existing_playlist_reports_duplicate_without_losing_metadata():
    window = _WindowStub()
    window.playlist_widget.add_results = [False]
    controller = _controller(window)
    messages = []

    controller._add_single_to_existing_playlist(
        "playlist-id",
        "Target",
        "video.mp4",
        "Video",
        "original.mp4",
        {"type": "video", "key_symbol": "lff", "track": 2},
        messages.append,
    )

    assert len(window.playlist_widget.added) == 1
    playlist_id, item = window.playlist_widget.added[0]
    assert playlist_id == "playlist-id"
    assert item["title"] == "Video"
    assert item["url"] == "video.mp4"
    assert item["key_symbol"] == "lff"
    assert item["track"] == 2
    assert item["original_filename"] == "original.mp4"
    assert messages == ['This media is already in\nplaylist "Target"']


def test_create_playlist_with_all_items_discards_tmp_files_and_counts_added():
    window = _WindowStub()
    window.playlist_widget.add_results = [False, True]
    window.wifi_receive_widget._wifi_tmp_files = {"a.mp4", "b.mp4", "c.mp4"}
    controller = _controller(window)
    messages = []

    controller._create_playlist_with_all_items(
        "Batch",
        [
            {"path": "a.mp4", "title": "A", "type": "video"},
            {"path": "b.mp4", "title": "B", "type": "video"},
            {"path": "c.mp4", "title": "C", "type": "video"},
        ],
        messages.append,
    )

    assert window.playlist_widget.created[0][0] == "Batch"
    assert [item["title"] for _, item in window.playlist_widget.added] == ["B", "C"]
    assert window.wifi_receive_widget._wifi_tmp_files == {"b.mp4"}
    assert messages == ['Playlist "Batch" created\nwith 2 file(s)!']


def test_wifi_playlist_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")


def test_wifi_receive_widget_sends_metadata_copies_for_all_received_items():
    emitted = []
    entry = {
        "path": "song.mp3",
        "title": "Song",
        "type": "audio",
        "key_symbol": "sjjm",
        "track": 12,
    }
    widget = SimpleNamespace(
        _received_files=[entry],
        request_add_all_to_playlist=SimpleNamespace(emit=emitted.append),
    )

    WifiReceiveWidget._on_send_all(widget)

    assert emitted == [[entry]]
    assert emitted[0][0] is not entry


def test_wifi_receive_widget_exposes_copies_and_transfers_temp_ownership():
    entry = {"path": "clip.mp4", "title": "Clip"}
    widget = SimpleNamespace(
        _received_files=[entry],
        _wifi_tmp_files={"clip.mp4", "other.mp4"},
    )

    received = WifiReceiveWidget.received_entry(widget, "clip.mp4")
    WifiReceiveWidget.preserve_temp_file(widget, "clip.mp4")

    assert received == entry
    assert received is not entry
    assert widget._wifi_tmp_files == {"other.mp4"}
