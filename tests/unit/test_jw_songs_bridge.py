from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.jw.songs import JWSongsStore
from solin.core.media.insertion import MediaInsertPayload, MediaInsertResult
from solin.ui.qml.jw_songs import JWSongsBridge


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def test_songs_bridge_filters_by_number_prefix_and_title(tmp_path):
    _app()
    bridge = JWSongsBridge(
        JWSongsStore(tmp_path),
        insertion_handler=lambda item, _list, _index: MediaInsertResult(added_items=(item,)),
    )
    try:
        bridge._all_items = [
            {"number": 2, "title": "Good Song", "url": "https://example.test/2.mp4"},
            {"number": 12, "title": "Kindness", "url": "https://example.test/12.mp4"},
            {"number": 120, "title": "Loyal Love", "url": "https://example.test/120.mp4"},
        ]
        bridge._apply_filter()

        bridge.setSearchQuery("12")

        assert bridge.resultCount == 2
        assert bridge.model.rowCount() == 2

        bridge.setSearchQuery("kind")

        assert bridge.resultCount == 1
        assert bridge.model.item_at(0)["title"] == "Kindness"
    finally:
        bridge.cleanup()


def test_songs_bridge_emits_playlist_ready_song_metadata(tmp_path):
    _app()
    emitted: list[tuple[MediaInsertPayload, str, int]] = []

    def insert(item, list_id, index):
        emitted.append((item, list_id, index))
        return MediaInsertResult(added_items=(item,))

    bridge = JWSongsBridge(JWSongsStore(tmp_path), insertion_handler=insert)
    try:
        bridge.set_language_context(
            api_code="T",
            fallback_code="T",
            is_sign_language=False,
        )
        bridge._pending_item = {
            "number": 2,
            "title": "Good Song",
            "url": "https://akamd1.jw-cdn.org/sg2/p/64dbe62/2/o/sjjm_T_002_r720P.mp4",
            "duration": 123,
            "thumbnail_url": "https://cdn.example/song.jpg",
        }

        bridge._submit_pending("root", 0)

        item, list_id, index = emitted[0]
        assert list_id == "root"
        assert index == 0
        assert item.title == "2. Good Song"
        assert item.source_url.endswith("sjjm_T_002_r720P.mp4")
        assert item.media_type == "video"
        assert item.key_symbol == "sjjm"
        assert item.track == 2
        assert item.language == "T"
        assert item.meps_language > 0
        assert item.base_duration_ticks == 1_230_000_000
        assert item.thumbnail_url == "https://cdn.example/song.jpg"
    finally:
        bridge.cleanup()


def test_songs_bridge_exposes_local_thumbnail_as_file_url(tmp_path):
    _app()
    bridge = JWSongsBridge(
        JWSongsStore(tmp_path),
        insertion_handler=lambda item, _list, _index: MediaInsertResult(added_items=(item,)),
    )
    try:
        thumbnail = tmp_path / "song cover.jpg"
        bridge._pending_item = {"thumbnail_path": str(thumbnail)}

        assert bridge.pendingItemThumb.startswith("file:///")
        assert bridge.pendingItemThumb.endswith("/song cover.jpg")
    finally:
        bridge.cleanup()


def test_songs_bridge_waits_for_official_thumbnail_before_insertion(tmp_path):
    _app()
    inserted: list[MediaInsertPayload] = []

    class ThumbnailSession(QObject):
        ready = Signal(str, str, str)

        def __init__(self):
            super().__init__()
            self.requests: list[tuple[str, str]] = []

        def enqueue(self, item_id, thumbnail_url):
            self.requests.append((item_id, thumbnail_url))
            return True

        def close(self):
            pass

    session = ThumbnailSession()

    class ThumbnailFactory:
        def create(self, *, parent=None):
            session.setParent(parent)
            return session

    def insert(item, _list_id, _index):
        inserted.append(item)
        return MediaInsertResult(added_items=(item,))

    bridge = JWSongsBridge(
        JWSongsStore(tmp_path),
        insertion_handler=insert,
        thumbnail_session_factory=ThumbnailFactory(),
    )
    try:
        thumbnail_url = "https://cdn.example/song.jpg"
        thumbnail_path = tmp_path / "song.jpg"
        bridge._pending_item = {
            "number": 2,
            "title": "Good Song",
            "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r720P.mp4",
            "duration": 123,
            "thumbnail_url": thumbnail_url,
        }

        assert bridge._submit_pending("root", 0) is False
        assert inserted == []
        assert session.requests == [("2", thumbnail_url)]

        session.ready.emit("2", thumbnail_url, str(thumbnail_path))

        assert len(inserted) == 1
        assert inserted[0].thumbnail_path == str(thumbnail_path)
        assert inserted[0].base_duration_ticks == 1_230_000_000
    finally:
        bridge.cleanup()


def test_songs_bridge_rejects_duplicate_before_placement(tmp_path):
    _app()
    insertions = []
    bridge = JWSongsBridge(
        JWSongsStore(tmp_path),
        insertion_handler=lambda *args: insertions.append(args),
    )
    try:
        song = {
            "number": 2,
            "title": "Good Song",
            "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r720P.mp4",
        }
        bridge._model.set_items_if_changed([song])
        bridge.set_playlist_ref(
            {
                "items": [{"url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r480P.mp4"}],
                "sections": [],
            }
        )
        rejected = []
        bridge.mediaAlreadyAdded.connect(lambda *args: rejected.append(args))

        bridge.selectItem(0)

        assert insertions == []
        assert rejected[0][0] == "2. Good Song"
        assert bridge._pending_item is None
        assert bridge.showPlacement is False
    finally:
        bridge.cleanup()


def test_songs_store_coalesces_matching_inflight_requests(tmp_path):
    _app()
    store = JWSongsStore(tmp_path)
    starts = []

    class _Pool:
        def start(self, worker):
            starts.append(worker)

    store._thread_pool = _Pool()

    request = store.request_for(
        api_code="T",
        fallback_code="T",
        is_sign_language=False,
        audio_mode=False,
    )

    first_key = store.ensure_loaded(request)
    second_key = store.ensure_loaded(request)

    assert first_key == second_key
    assert len(starts) == 1
    assert len(store._workers) == 1
