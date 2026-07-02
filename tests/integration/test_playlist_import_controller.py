from types import SimpleNamespace

from solin.controllers.playlist_import_controller import (
    PlaylistImportContext,
    PlaylistImportController,
    PlaylistImportHandlers,
)
from solin.core.media.destinations import (
    MediaDestinationAsset,
    PlaylistDestinationTarget,
)
from solin.core.foundation.qt_threads import OwnedQThreadRegistry
from solin.core.playlists.items import PlaylistInsertResult


class _PlaylistWidgetStub:
    def __init__(self):
        self.created = []
        self.add_results = []
        self.added = []
        self.temp_opened = None
        self.target_valid = True

    def get_playlist_names(self):
        return ["Existing"]

    def create_playlist_with_items(self, name, items):
        self.created.append((name, list(items)))
        return "new-playlist-id"

    def add_items_to_playlist(
        self,
        playlist_id,
        items,
        *,
        list_id,
        insert_index,
    ):
        if not self.target_valid:
            return PlaylistInsertResult(target_valid=False)
        added = []
        duplicates = 0
        for item in items:
            self.added.append((playlist_id, item, list_id, insert_index))
            was_added = self.add_results.pop(0) if self.add_results else True
            if was_added:
                added.append(item)
            else:
                duplicates += 1
        return PlaylistInsertResult(tuple(added), duplicates)

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


def _target(
    create_new=False,
    *,
    list_id="root",
    insert_index=2**31 - 1,
):
    return PlaylistDestinationTarget(
        create_new=create_new,
        playlist_name="Target",
        playlist_id="playlist-id",
        list_id=list_id,
        insert_index=insert_index,
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


def test_add_items_to_new_playlist_preserves_complete_media_item():
    window = _WindowStub()
    controller = _controller(window)

    outcome = controller.add_items_to_playlist_target(
        _target(create_new=True),
        [
            {
                "title": "Song",
                "url": "song.mp3",
                "type": "audio",
                "track": 3,
            }
        ],
        "song.mp3",
    )

    assert window.playlist_widget.created[0][0] == "Target"
    item = window.playlist_widget.created[0][1][0]
    assert item["title"] == "Song"
    assert item["url"] == "song.mp3"
    assert item["type"] == "audio"
    assert item["track"] == 3
    assert outcome.added_count == 1
    assert outcome.referenced_urls == ("song.mp3",)
    assert window.notifications.events == [
        ("success", 'Playlist "Target"\ncreated successfully!', {})
    ]


def test_add_items_to_existing_playlist_reports_duplicate_when_add_fails():
    window = _WindowStub()
    window.playlist_widget.add_results = [False]
    controller = _controller(window)
    outcome = controller.add_items_to_playlist_target(
        _target(),
        [{"title": "Video", "url": "video.mp4", "type": "video"}],
        "video.mp4",
    )

    assert window.playlist_widget.added[0][0] == "playlist-id"
    assert outcome.added_count == 0
    assert outcome.referenced_urls == ()
    assert window.notifications.events == [
        ("warning", 'This media is already in\nplaylist "Target"', {})
    ]


def test_add_items_to_playlist_target_counts_added_items():
    window = _WindowStub()
    window.playlist_widget.add_results = [True, False, True]
    controller = _controller(window)

    outcome = controller.add_items_to_playlist_target(
        _target(list_id="section:talk", insert_index=0),
        [
            {"title": "A", "url": "a.mp4"},
            {"title": "B", "url": "b.mp4"},
            {"title": "C", "url": "c.mp4"},
        ],
        "source",
    )

    assert len(window.playlist_widget.added) == 3
    assert {
        (entry[2], entry[3]) for entry in window.playlist_widget.added
    } == {("section:talk", 0)}
    assert outcome.added_count == 2
    assert outcome.referenced_urls == ("a.mp4", "c.mp4")
    assert window.notifications.events == [
        ("success", '2 file(s) added\nto playlist "Target"', {})
    ]


def test_add_items_rejects_a_stale_playlist_placement():
    window = _WindowStub()
    window.playlist_widget.target_valid = False
    controller = _controller(window)

    outcome = controller.add_items_to_playlist_target(
        _target(list_id="section:removed", insert_index=0),
        [{"title": "A", "url": "a.mp4"}],
        "source",
    )

    assert outcome.destination_accepted is False
    assert outcome.added_count == 0
    assert window.notifications.events[0][0] == "error"


def test_prepare_destination_assets_preserves_direct_and_import_order(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
    results = []
    monkeypatch.setattr(
        "solin.controllers.playlist_import_controller.os.path.isfile",
        lambda path: True,
    )
    monkeypatch.setattr(
        controller,
        "items_from_jwlplaylist_for_playlist",
        lambda path: [{"title": "Imported", "url": "imported.mp4"}],
    )

    controller.prepare_destination_assets(
        [
            MediaDestinationAsset(
                title="Direct",
                source_id="direct.mp4",
                item={"title": "Direct", "url": "direct.mp4"},
            ),
            MediaDestinationAsset(
                title="Playlist",
                source_id="playlist.jwlplaylist",
                import_path="playlist.jwlplaylist",
                import_kind="jwlplaylist",
            ),
        ],
        results.append,
    )

    assert [item["title"] for item in results[0].items] == [
        "Direct",
        "Imported",
    ]
    assert results[0].handled_sources == (
        "direct.mp4",
        "playlist.jwlplaylist",
    )


def test_large_direct_batch_is_prepared_without_recursive_dispatch():
    controller = _controller(_WindowStub())
    assets = [
        MediaDestinationAsset(
            title=f"Item {index}",
            source_id=f"{index}.mp4",
            item={"title": f"Item {index}", "url": f"{index}.mp4"},
        )
        for index in range(1500)
    ]
    results = []

    controller.prepare_destination_assets(assets, results.append)

    assert len(results[0].items) == 1500
    assert results[0].items[-1]["url"] == "1499.mp4"


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


def test_pdf_preparation_uses_injected_document_conversion_service(monkeypatch):
    window = _WindowStub()
    service = _DocumentConversionServiceStub()
    controller = _controller(
        window,
        document_conversion_service=service,
    )

    monkeypatch.setattr(
        "solin.controllers.playlist_import_controller.os.path.isfile",
        lambda path: True,
    )
    results = []
    controller.prepare_destination_assets(
        [
            MediaDestinationAsset(
                title="Document",
                source_id="document.pdf",
                import_path="document.pdf",
                import_kind="pdf",
            )
        ],
        results.append,
    )

    assert service.pdf_calls == [("document.pdf", {"parent": window})]
    assert service.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1
    service.threads[0].pages_ready.callbacks[0](["page-1.png"], "Document")
    assert results[0].items[0]["url"] == "page-1.png"
    assert results[0].handled_sources == ("document.pdf",)


def test_jwpub_preparation_uses_injected_worker_factory(monkeypatch):
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

    monkeypatch.setattr(
        "solin.controllers.playlist_import_controller.os.path.isfile",
        lambda path: True,
    )
    results = []
    controller.prepare_destination_assets(
        [
            MediaDestinationAsset(
                title="Publication",
                source_id="publication.jwpub",
                import_path="publication.jwpub",
                import_kind="jwpub",
            )
        ],
        results.append,
    )

    assert factory.calls == [
        (
            "publication.jwpub",
            {"lang": "T", "parent": window},
        )
    ]
    assert factory.threads[0].started is True
    assert controller._context.thread_registry.active_count == 1
    factory.threads[0].items_ready.callbacks[0](
        [{"title": "Video", "url": "video.mp4", "type": "video"}],
        "Publication",
    )
    assert results[0].items[0]["url"] == "video.mp4"
    assert results[0].handled_sources == ("publication.jwpub",)


def test_playlist_import_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
