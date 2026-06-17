from types import SimpleNamespace

from solin.controllers.playlist_import_controller import (
    PlaylistImportContext,
    PlaylistImportController,
    PlaylistImportHandlers,
)
from solin.core.foundation.qt_threads import OwnedQThreadRegistry


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


class _ProfileMediaStoreStub:
    def __init__(self):
        self.saved = []

    def save_embedded(
        self,
        data,
        filename_hint="media",
        *,
        identifier=None,
        default_suffix=".mp4",
    ):
        self.saved.append((data, filename_hint, identifier, default_suffix))
        return f"embedded/{identifier}{default_suffix}"


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)
        return callback


class _ThreadStub:
    def __init__(self):
        self.items_ready = _SignalStub()
        self.failed = _SignalStub()
        self.pages_ready = _SignalStub()
        self.conversion_failed = _SignalStub()
        self.finished = _SignalStub()
        self.started = False

    def start(self):
        self.started = True


class _JwpubImportThreadFactoryStub:
    def __init__(self):
        self.calls = []
        self.threads = []

    def create(self, path, **kwargs):
        thread = _ThreadStub()
        self.calls.append((path, kwargs))
        self.threads.append(thread)
        return thread


class _DocumentConversionServiceStub:
    def __init__(self):
        self.pdf_pages = None
        self.pdf_calls = []
        self.threads = []

    def cached_pdf_pages(self, path):
        return self.pdf_pages

    def create_pdf_thread(self, path, **kwargs):
        thread = _ThreadStub()
        self.pdf_calls.append((path, kwargs))
        self.threads.append(thread)
        return thread


def _target(create_new=False):
    return SimpleNamespace(
        create_new=create_new,
        playlist_name="Target",
        playlist_id="playlist-id",
    )


def _controller(
    window,
    profile_media_store=None,
    jwpub_import_thread_factory=None,
    document_conversion_service=None,
):
    return PlaylistImportController(
        PlaylistImportContext(
            dialog_parent=window,
            document_conversion_service=(
                document_conversion_service
                or _DocumentConversionServiceStub()
            ),
            profile_media_store=profile_media_store or _ProfileMediaStoreStub(),
            jwpub_import_thread_factory=(
                jwpub_import_thread_factory
                or _JwpubImportThreadFactoryStub()
            ),
            language_manager=window.lang,
            notifications=window.notifications,
            playlist_widget=window.playlist_widget,
            thread_registry=OwnedQThreadRegistry(),
            translate=window.tr,
        ),
        PlaylistImportHandlers(
            switch_to_playlist=lambda: window._navigation.switch_page(7),
        ),
    )


def test_add_current_to_playlist_creates_new_playlist(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
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
    controller = _controller(window)
    monkeypatch.setattr(controller, "_choose_target", lambda title: _target())

    controller.add_current_to_playlist("video.mp4", "Video", {})

    assert window.playlist_widget.added[0][0] == "playlist-id"
    assert window.notifications.events == [
        ("warning", 'This media is already in\nplaylist "Target"', {})
    ]


def test_add_items_to_playlist_target_counts_added_items():
    window = _WindowStub()
    window.playlist_widget.add_results = [True, False, True]
    controller = _controller(window)

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
    controller = _controller(window)
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


def test_items_from_jwlplaylist_persists_embedded_media_via_profile_store(
    monkeypatch,
):
    window = _WindowStub()
    profile_media_store = _ProfileMediaStoreStub()
    controller = _controller(window, profile_media_store=profile_media_store)
    monkeypatch.setattr(
        "solin.core.playlists.jwl_files.read_jwlplaylist",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "title": "Embedded clip",
                    "type": "video",
                    "data": b"video",
                    "filename": "clip.mp4",
                    "mime_type": "video/mp4",
                }
            ]
        },
    )

    items = controller.items_from_jwlplaylist_for_playlist("playlist.jwlplaylist")

    assert len(items) == 1
    assert items[0]["url"].startswith("embedded/")
    assert profile_media_store.saved == [
        (b"video", "clip.mp4", items[0]["id"], ".mp4")
    ]


def test_send_to_temp_playlist_switches_to_playlist_page():
    window = _WindowStub()
    controller = _controller(window)
    items = [{"title": "Item"}]

    controller.send_to_temp_playlist(items)

    assert window._navigation.pages == [7]
    assert window.playlist_widget.temp_opened == (items, window.lang)


def test_pdf_import_uses_injected_document_conversion_service():
    window = _WindowStub()
    service = _DocumentConversionServiceStub()
    controller = _controller(
        window,
        document_conversion_service=service,
    )

    controller.add_pdf_file_to_playlist_target("document.pdf", _target())

    assert service.pdf_calls == [("document.pdf", {"parent": window})]
    assert service.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1


def test_jwpub_import_uses_injected_worker_factory(monkeypatch):
    window = _WindowStub()
    factory = _JwpubImportThreadFactoryStub()
    controller = _controller(
        window,
        jwpub_import_thread_factory=factory,
    )
    monkeypatch.setattr(
        controller,
        "_media_language_context",
        lambda: SimpleNamespace(api_code="T"),
    )

    controller.add_jwpub_file_to_playlist_target(
        "publication.jwpub",
        _target(),
    )

    assert factory.calls == [
        (
            "publication.jwpub",
            {"lang": "T", "parent": window},
        )
    ]
    assert factory.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1


def test_playlist_import_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
