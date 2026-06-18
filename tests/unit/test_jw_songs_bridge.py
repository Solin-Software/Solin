from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.jw.songs import JWSongsStore
from solin.ui.qml.jw_songs import JWSongsBridge


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def test_songs_bridge_filters_by_number_prefix_and_title(tmp_path):
    _app()
    bridge = JWSongsBridge(JWSongsStore(tmp_path))
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
    bridge = JWSongsBridge(JWSongsStore(tmp_path))
    try:
        bridge.set_language_context(
            api_code="T",
            fallback_code="T",
            is_sign_language=False,
        )
        emitted: list[tuple[dict, str, int]] = []
        bridge.jwMediaConfirmed.connect(
            lambda item, list_id, index: emitted.append((item, list_id, index))
        )
        bridge._pending_item = {
            "number": 2,
            "title": "Good Song",
            "url": "https://akamd1.jw-cdn.org/sg2/p/64dbe62/2/o/sjjm_T_002_r720P.mp4",
            "duration": 123,
        }

        bridge._emit_confirmed("root", 0)

        item, list_id, index = emitted[0]
        assert list_id == "root"
        assert index == 0
        assert item["title"] == "2. Good Song"
        assert item["download_url"].endswith("sjjm_T_002_r720P.mp4")
        assert item["media_type"] == "video"
        assert item["pub"] == "sjjm"
        assert item["track"] == 2
        assert item["language"] == "T"
        assert item["meps_language"] > 0
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
