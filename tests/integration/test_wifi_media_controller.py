from types import SimpleNamespace

from solin.controllers.wifi_media_controller import (
    WifiMediaContext,
    WifiMediaController,
    WifiMediaHandlers,
)
from solin.core.media.destinations import MediaDestinationOutcome
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
        self.video_calls.append((path, title, playlist, playback_order, from_saved_playlist))


class _DestinationStub:
    def __init__(self):
        self.routes = []

    def route(self, request, **callbacks):
        self.routes.append((request, callbacks))


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


class _WindowStub:
    def __init__(self):
        self._media_projection = _MediaProjectionStub()
        self.destination_controller = _DestinationStub()
        self.wifi_receive_widget = _WifiReceiveWidgetStub()


def _controller(window):
    return WifiMediaController(
        WifiMediaContext(
            destination_controller=window.destination_controller,
            wifi_receive_widget=lambda: window.wifi_receive_widget,
            translate=lambda text: text,
        ),
        WifiMediaHandlers(
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

    assert window._media_projection.cache_calls == [("slide.png", "image", "", "Slide")]
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
    controller = _controller(_WindowStub())

    item = controller.item_from_wifi_entry(
        {
            "path": "song.mp3",
            "title": "Song",
            "type": "audio",
            "key_symbol": "sjjm",
            "track": 12,
            "issue_tag": None,
            "doc_id": "abc",
            "meps_language": 3,
            "orig_name": "original.mp3",
        }
    )

    assert item["title"] == "Song"
    assert item["url"] == "song.mp3"
    assert item["type"] == "audio"
    assert item["key_symbol"] == "sjjm"
    assert item["track"] == 12
    assert item["doc_id"] == "abc"
    assert item["meps_language"] == 3
    assert item["original_filename"] == "original.mp3"
    assert "issue_tag" not in item or item["issue_tag"] is None


def test_single_destination_completion_transfers_only_referenced_file():
    window = _WindowStub()
    window.wifi_receive_widget._received_files = [
        {
            "path": "clip.mp4",
            "title": "Clip",
            "type": "video",
        }
    ]
    window.wifi_receive_widget._wifi_tmp_files = {"clip.mp4"}
    controller = _controller(window)

    controller.on_wifi_request_add_single("clip.mp4", "Clip", "original.mp4")

    request, callbacks = window.destination_controller.routes[0]
    assert request.assets[0].item["original_filename"] == "original.mp4"
    callbacks["completed"](MediaDestinationOutcome(1, ("clip.mp4",), ("clip.mp4",)))
    assert window.wifi_receive_widget._wifi_tmp_files == set()
    assert window.wifi_receive_widget.removed == ["clip.mp4"]


def test_batch_destination_preserves_added_files_and_clears_only_after_success():
    window = _WindowStub()
    entries = [
        {"path": "a.mp4", "title": "A", "type": "video"},
        {"path": "b.mp4", "title": "B", "type": "video"},
    ]
    window.wifi_receive_widget._wifi_tmp_files = {"a.mp4", "b.mp4"}
    controller = _controller(window)

    controller.on_wifi_add_all(entries)

    assert window.wifi_receive_widget.clear_all_count == 0
    _request, callbacks = window.destination_controller.routes[0]
    callbacks["completed"](MediaDestinationOutcome(1, ("a.mp4",), ("a.mp4",)))
    assert window.wifi_receive_widget._wifi_tmp_files == {"b.mp4"}
    assert window.wifi_receive_widget.removed == ["a.mp4"]
    assert window.wifi_receive_widget.clear_all_count == 0


def test_failed_destination_keeps_wifi_card_and_temporary_file():
    window = _WindowStub()
    window.wifi_receive_widget._received_files = [
        {"path": "document.pdf", "title": "Document", "type": "document"}
    ]
    window.wifi_receive_widget._wifi_tmp_files = {"document.pdf"}
    controller = _controller(window)

    controller.on_wifi_request_add_single("document.pdf", "Document", "document.pdf")
    request, callbacks = window.destination_controller.routes[0]
    assert request.assets[0].import_kind == "pdf"
    assert request.assets[0].import_path == "document.pdf"

    callbacks["completed"](MediaDestinationOutcome(0))

    assert window.wifi_receive_widget._wifi_tmp_files == {"document.pdf"}
    assert window.wifi_receive_widget.removed == []


def test_wifi_media_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")


def test_wifi_receive_widget_sends_metadata_copies_without_eager_cleanup():
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
        request_add_all_to_destination=SimpleNamespace(emit=emitted.append),
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
