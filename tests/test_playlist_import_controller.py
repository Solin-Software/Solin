from types import SimpleNamespace

from solin.controllers.playlist_import_controller import PlaylistImportController
from solin.core.foundation.runtime_paths import ProfilePaths

_PROFILE_PATHS = ProfilePaths.from_roots(
    data_dir="data",
    cache_dir="cache",
    profile_id="test",
)


class _PlaylistWidgetStub:
    def __init__(self):
        self.created = []
        self.add_results = []
        self.added = []
        self.temp_opened = None

    def get_playlist_names(self):
        return ["Existing"]

    def create_playlist_with_item(self, name, item):
        self.created.append((name, item))
        return "new-playlist-id"

    def add_item_to_playlist(self, playlist_id, item):
        self.added.append((playlist_id, item))
        return self.add_results.pop(0) if self.add_results else True

    def open_temp_playlist(self, items, lang):
        self.temp_opened = (items, lang)


class _NotificationsStub:
    def __init__(self):
        self.events = []

    def success(self, message, **kwargs):
        self.events.append(("success", message, kwargs))

    def warning(self, message, **kwargs):
        self.events.append(("warning", message, kwargs))

    def error(self, message, **kwargs):
        self.events.append(("error", message, kwargs))

    def information(self, message, **kwargs):
        self.events.append(("information", message, kwargs))


class _NavigationStub:
    def __init__(self):
        self.pages = []

    def switch_page(self, index):
        self.pages.append(index)


class _WindowStub:
    def __init__(self):
        self.playlist_widget = _PlaylistWidgetStub()
        self.notifications = _NotificationsStub()
        self._navigation = _NavigationStub()
        self.lang = object()

    def tr(self, text):
        return text


def _target(create_new=False):
    return SimpleNamespace(
        create_new=create_new,
        playlist_name="Target",
        playlist_id="playlist-id",
    )


def test_add_current_to_playlist_creates_new_playlist(monkeypatch):
    window = _WindowStub()
    controller = PlaylistImportController(window, _PROFILE_PATHS)
    monkeypatch.setattr(controller, "_choose_target", lambda title: _target(create_new=True))

    controller.add_current_to_playlist(
        "song.mp3",
        "Song",
        {"type": "audio", "track": 3},
    )

    assert window.playlist_widget.created[0][0] == "Target"
    item = window.playlist_widget.created[0][1]
    assert item["title"] == "Song"
    assert item["url"] == "song.mp3"
    assert item["type"] == "audio"
    assert item["track"] == 3
    assert window.notifications.events == [
        ("success", 'Playlist "Target"\ncreated successfully!', {})
    ]


def test_add_current_to_playlist_reports_duplicate_when_existing_add_fails(monkeypatch):
    window = _WindowStub()
    window.playlist_widget.add_results = [False]
    controller = PlaylistImportController(window, _PROFILE_PATHS)
    monkeypatch.setattr(controller, "_choose_target", lambda title: _target())

    controller.add_current_to_playlist("video.mp4", "Video", {})

    assert window.playlist_widget.added[0][0] == "playlist-id"
    assert window.notifications.events == [
        ("warning", 'This media is already in\nplaylist "Target"', {})
    ]


def test_add_items_to_playlist_target_counts_added_items():
    window = _WindowStub()
    window.playlist_widget.add_results = [True, False, True]
    controller = PlaylistImportController(window, _PROFILE_PATHS)

    controller.add_items_to_playlist_target(
        _target(),
        [{"title": "A"}, {"title": "B"}, {"title": "C"}],
        "source",
    )

    assert len(window.playlist_widget.added) == 3
    assert window.notifications.events == [
        ("success", '2 file(s) added\nto playlist "Target"', {})
    ]


def test_add_browser_downloaded_file_routes_by_kind(monkeypatch):
    window = _WindowStub()
    controller = PlaylistImportController(window, _PROFILE_PATHS)
    calls = []
    monkeypatch.setattr(
        "solin.controllers.playlist_import_controller.os.path.isfile",
        lambda path: True,
    )
    monkeypatch.setattr(controller, "_choose_target", lambda title: _target())
    monkeypatch.setattr(
        controller,
        "items_from_jwlplaylist_for_playlist",
        lambda path: [{"title": "Item"}],
    )
    monkeypatch.setattr(
        controller,
        "add_items_to_playlist_target",
        lambda target, items, source_name: calls.append(("items", items, source_name)),
    )
    monkeypatch.setattr(
        controller,
        "add_pdf_file_to_playlist_target",
        lambda path, target: calls.append(("pdf", path)),
    )
    monkeypatch.setattr(
        controller,
        "add_jwpub_file_to_playlist_target",
        lambda path, target: calls.append(("jwpub", path)),
    )

    controller.add_browser_downloaded_file("playlist.jwlplaylist", "Playlist", "jwlplaylist")
    controller.add_browser_downloaded_file("doc.pdf", "PDF", "pdf")
    controller.add_browser_downloaded_file("pub.jwpub", "JWPUB", "jwpub")

    assert calls == [
        ("items", [{"title": "Item"}], "playlist.jwlplaylist"),
        ("pdf", "doc.pdf"),
        ("jwpub", "pub.jwpub"),
    ]


def test_send_to_temp_playlist_switches_to_playlist_page():
    window = _WindowStub()
    controller = PlaylistImportController(window, _PROFILE_PATHS)
    items = [{"title": "Item"}]

    controller.send_to_temp_playlist(items)

    assert window._navigation.pages == [7]
    assert window.playlist_widget.temp_opened == (items, window.lang)
