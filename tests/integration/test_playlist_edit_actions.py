from types import SimpleNamespace

from solin.widgets.playlist import widget as playlist_widget
from solin.widgets.playlist.edit_actions import PlaylistEditActionsMixin


def test_playlist_edit_view_uses_actions_mixin():
    assert playlist_widget.PlaylistEditActionsMixin is PlaylistEditActionsMixin
    assert issubclass(playlist_widget.PlaylistEditView, PlaylistEditActionsMixin)
    assert playlist_widget.PlaylistEditView._add_files is PlaylistEditActionsMixin._add_files
    assert playlist_widget.PlaylistEditView.load_temp_playlist is (
        PlaylistEditActionsMixin.load_temp_playlist
    )


def test_temp_playlist_sessions_receive_unique_ids():
    loaded = []
    view = SimpleNamespace(
        lang=None,
        tr=lambda text: text,
        load_playlist=loaded.append,
    )

    first = PlaylistEditActionsMixin.load_temp_playlist(view, [])
    second = PlaylistEditActionsMixin.load_temp_playlist(view, [])

    assert first != second
    assert loaded[0]["id"] == first
    assert loaded[1]["id"] == second


def test_append_temp_playlist_items_rejects_stale_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _rebuild_list=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "stale",
        [{"title": "Late result"}],
    )

    assert appended is False
    assert edit_view._pl["items"] == []
    assert rebuilds == []


def test_append_temp_playlist_items_updates_matching_session():
    rebuilds = []
    edit_view = SimpleNamespace(
        _is_temp=True,
        _pl={"id": "current", "items": []},
        _rebuild_list=lambda: rebuilds.append(True),
    )
    widget = SimpleNamespace(_edit_view=edit_view)
    item = {"title": "Imported"}

    appended = playlist_widget.PlaylistWidget.append_temp_playlist_items(
        widget,
        "current",
        [item],
    )
    item["title"] = "Mutated externally"

    assert appended is True
    assert edit_view._pl["items"] == [{"title": "Imported"}]
    assert rebuilds == [True]


def test_jw_duplicate_is_rejected_before_thumbnail_or_playlist_mutation(tmp_path):
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"thumb")
    thumbnail_copies = []
    existing = {
        "id": "existing",
        "title": "Song",
        "url": "https://akamd1.jw-cdn.org/x/sjjm_T_002_r480P.mp4",
        "key_symbol": "sjjm",
        "track": 2,
        "meps_language": 5,
    }
    view = SimpleNamespace(
        _pl={"id": "playlist", "items": [existing]},
        _playlist_thumbnail_store=SimpleNamespace(
            copy_from=lambda *args: thumbnail_copies.append(args)
        ),
    )

    result = PlaylistEditActionsMixin._on_jw_media_confirmed(
        view,
        {
            "title": "2. Song",
            "download_url": "https://akamd1.jw-cdn.org/y/sjjm_T_002_r720P.mp4",
            "media_type": "video",
            "pub": "sjjm",
            "track": 2,
            "language": "T",
            "meps_language": 5,
            "thumbnail_path": str(thumbnail),
        },
        "root",
        0,
    )

    assert result.added_count == 0
    assert result.duplicate_count == 1
    assert view._pl["items"] == [existing]
    assert thumbnail_copies == []


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)
        return callback

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class _SyncThread:
    def __init__(self):
        self.sync_complete = _Signal()
        self.sync_failed = _Signal()
        self.finished = _Signal()
        self.started = False

    def start(self):
        self.started = True

    def deleteLater(self):
        pass


class _WatchedFolderStore:
    def __init__(self, thread):
        self.thread = thread

    def pending_files(self, _path):
        return ["slides.pdf"]

    def create_sync_thread(self, *_args, **_kwargs):
        return self.thread


class _Notifications:
    def __init__(self):
        self.infos = []
        self.successes = []
        self.errors = []

    def information(self, message):
        self.infos.append(message)

    def success(self, message):
        self.successes.append(message)

    def error(self, message):
        self.errors.append(message)


def test_watched_folder_sync_is_quiet_unless_it_fails():
    thread = _SyncThread()
    notifications = _Notifications()
    refreshes = []
    view = SimpleNamespace(
        _watched_path="folder",
        _watched_folder_playlist_store=_WatchedFolderStore(thread),
        _notifications=notifications,
        _wf_refresh_pending=False,
        _wf_sync_thread=None,
        _current_media_context=lambda: SimpleNamespace(api_code="E", fallback_code="T"),
        tr=lambda text, *_args: text,
        refresh_watched_folder=lambda: refreshes.append(True),
    )

    playlist_widget.PlaylistEditView._start_wf_sync(view)
    thread.sync_complete.emit()
    thread.sync_failed.emit("disk full")

    assert thread.started is True
    assert refreshes == [True]
    assert notifications.infos == []
    assert notifications.successes == []
    assert notifications.errors == ["disk full"]
