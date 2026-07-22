import os
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


def test_existing_thumbnail_does_not_skip_missing_duration_hydration():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    requests = []
    view = SimpleNamespace(
        _id_to_thumb={"media-1": _Thumbnail()},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: True),
        _request_thumbnail=lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    item = {
        "id": "media-1",
        "url": "clip.mp4",
        "type": "video",
    }

    playlist_widget.PlaylistEditView._request_missing_thumbnail_for_item(view, item)

    assert requests == [
        (
            ("media-1", "clip.mp4", "video"),
            {
                "require_thumbnail": False,
                "require_title": False,
                "require_duration": True,
            },
        )
    ]


def test_rebuild_keeps_matching_thumbnail_request_and_applies_late_result(
    monkeypatch,
):
    class _Pixmap:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    invalidated = []
    updated = []
    changed = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "type": "video",
        "base_duration_ticks": 10_000,
    }
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(True, False, False)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=updated.append,
            update_title=lambda *_args: None,
        ),
        bridge=SimpleNamespace(emit_media_changed=changed.append),
        _save=lambda: None,
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )
    view._thumbnail_result_is_current = lambda item_id, source: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
        )
    )
    monkeypatch.setattr(playlist_widget, "save_thumbnail", lambda *_args: None)

    pixmap = _Pixmap()
    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(
        view,
        [dict(item)],
    )
    playlist_widget.PlaylistEditView._on_info(view, 12, pixmap, "")

    assert invalidated == [12]
    assert view._thumb_idx_to_id == {}
    assert view._thumb_idx_to_source == {}
    assert view._thumb_idx_to_intent == {}
    assert view._thumb_pending_item_ids == set()
    assert view._id_to_thumb == {"media-1": pixmap}
    assert updated == ["media-1"]
    assert changed == ["media-1"]


def test_visual_rebuild_does_not_restart_thumbnail_work():
    invalidated = []
    rebuilt = []
    replacements = []
    playlist = {"items": [{"id": "media-1", "url": "C:/media/new.mp4"}]}
    view = SimpleNamespace(
        _pl=playlist,
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: "C:/media/old.mp4"},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(True, False, False)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(rebuild=rebuilt.append),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
        _request_thumbnail=lambda *args, **kwargs: replacements.append(
            (args, kwargs)
        ),
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    view._reconcile_thumbnail_requests = lambda items: (
        playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, items)
    )

    playlist_widget.PlaylistEditView._rebuild_model(view)

    assert invalidated == []
    assert view._thumb_idx_to_id == {12: "media-1"}
    assert view._thumb_idx_to_source == {12: "C:/media/old.mp4"}
    assert view._thumb_idx_to_intent == {
        12: playlist_widget._ThumbnailRequestIntent(True, False, False)
    }
    assert view._thumb_pending_item_ids == {"media-1"}
    assert rebuilt == [playlist]
    assert replacements == []


def test_rebuild_promotes_request_when_duration_becomes_required():
    invalidated = []
    replacements = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "type": "video",
    }
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(True, False, False)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
        _request_thumbnail=lambda *args, **kwargs: replacements.append(
            (args, kwargs)
        ),
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, [item])

    assert invalidated == [12]
    assert replacements == [
        (
            ("media-1", item["url"], "video"),
            {
                "require_thumbnail": True,
                "require_title": False,
                "require_duration": True,
            },
        )
    ]


def test_rebuild_retires_request_when_snapshot_satisfies_all_intents():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    invalidated = []
    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "Resolved title",
        "auto_title": False,
        "base_duration_ticks": 10_000,
    }
    view = SimpleNamespace(
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(True, True, True)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=invalidated.append),
        _id_to_thumb={"media-1": _Thumbnail()},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    playlist_widget.PlaylistEditView._reconcile_thumbnail_requests(view, [item])

    assert invalidated == [12]
    assert view._thumb_idx_to_id == {}
    assert view._thumb_idx_to_source == {}
    assert view._thumb_idx_to_intent == {}
    assert view._thumb_pending_item_ids == set()


def test_duration_only_result_does_not_rewrite_thumbnail_or_title():
    class _Thumbnail:
        @staticmethod
        def isNull():  # noqa: N802 - Qt-style test double
            return False

    item = {
        "id": "media-1",
        "url": "C:/media/clip.mp4",
        "title": "Original",
    }
    thumbnail_updates = []
    title_updates = []
    saves = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(False, False, True)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=thumbnail_updates.append,
            update_title=lambda *args: title_updates.append(args),
        ),
        bridge=SimpleNamespace(emit_media_changed=lambda _item_id: None),
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        _Thumbnail(),
        "Unexpected title",
    )

    assert view._id_to_thumb == {}
    assert item["title"] == "Original"
    assert thumbnail_updates == []
    assert title_updates == []
    assert saves == []


def test_equal_auto_title_is_marked_resolved_without_visual_patch():
    item = {
        "id": "media-1",
        "url": "https://example.test/clip.mp4",
        "title": "Resolved title",
        "auto_title": True,
    }
    title_updates = []
    saves = []
    view = SimpleNamespace(
        _pl={"items": [item]},
        _thumb_idx_to_id={12: "media-1"},
        _thumb_idx_to_source={12: item["url"]},
        _thumb_idx_to_intent={
            12: playlist_widget._ThumbnailRequestIntent(False, True, False)
        },
        _thumb_pending_item_ids={"media-1"},
        _thumb_queue=SimpleNamespace(invalidate=lambda _token: None),
        _id_to_thumb={},
        _playlist_thumbnail_store=SimpleNamespace(exists=lambda _item_id: False),
        model=SimpleNamespace(
            update_thumb=lambda _item_id: None,
            update_title=lambda *args: title_updates.append(args),
        ),
        bridge=SimpleNamespace(emit_media_changed=lambda _item_id: None),
        _save=lambda: saves.append(True),
        _same_media_source=playlist_widget.PlaylistEditView._same_media_source,
    )
    view._retire_thumbnail_request = lambda token: (
        playlist_widget.PlaylistEditView._retire_thumbnail_request(view, token)
    )
    view._thumbnail_result_is_current = lambda item_id, source: (
        playlist_widget.PlaylistEditView._thumbnail_result_is_current(
            view,
            item_id,
            source,
        )
    )
    view._thumbnail_request_intent = lambda current: (
        playlist_widget.PlaylistEditView._thumbnail_request_intent(view, current)
    )

    playlist_widget.PlaylistEditView._on_info(
        view,
        12,
        None,
        "Resolved title",
    )

    assert item["auto_title"] is False
    assert title_updates == []
    assert saves == [True]


def test_playlist_widget_records_source_duration_through_public_boundary():
    recorded = []
    widget = SimpleNamespace(
        _edit_view=SimpleNamespace(
            notify_duration=lambda item_id, duration: recorded.append((item_id, duration))
        )
    )

    playlist_widget.PlaylistWidget.record_source_duration(widget, "media-1", 12_345)
    playlist_widget.PlaylistWidget.record_source_duration(widget, "", 12_345)
    playlist_widget.PlaylistWidget.record_source_duration(widget, "media-1", 0)

    assert recorded == [("media-1", 12_345)]


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


def test_watcher_coalesces_events_and_defers_hidden_list_refresh():
    starts = []
    list_refreshes = []
    edit_refreshes = []
    superseded = []
    notifications = []
    folder_path = "C:/linked/one"
    widget = SimpleNamespace(
        _wf_root_refresh_pending=False,
        _wf_pending_sub_path="",
        _wf_refresh_debounce=SimpleNamespace(start=lambda: starts.append(True)),
        _list_view=SimpleNamespace(
            refresh_watched=lambda: list_refreshes.append(True)
        ),
        _stack=SimpleNamespace(currentIndex=lambda: 1),
        _edit_view=SimpleNamespace(
            _is_watched=True,
            _watched_path=folder_path,
            refresh_watched_folder=lambda: edit_refreshes.append(True),
            supersede_watched_folder_refresh=lambda: superseded.append(True),
        ),
        _watched_folder_playlist_store=SimpleNamespace(
            notify_external_change=lambda: notifications.append(True)
        ),
    )

    playlist_widget.PlaylistWidget._on_folder_changed(widget)
    playlist_widget.PlaylistWidget._on_subfolder_changed(widget, folder_path)
    playlist_widget.PlaylistWidget._flush_watched_folder_refresh(widget)

    assert starts == [True, True]
    assert superseded == [True, True]
    assert list_refreshes == []
    assert edit_refreshes == [True]
    assert notifications == [True]


def test_watched_folder_refresh_schedules_disk_snapshot_off_qt_thread():
    submitted = []

    class _Future:
        def add_done_callback(self, callback):
            self.callback = callback

    class _Executor:
        def submit(self, callback, *args):
            submitted.append((callback, args))
            return _Future()

    folder_path = "C:/linked/one"
    view = SimpleNamespace(
        _flush_image_framing_save=lambda: None,
        _wf_refresh_shutdown=False,
        _is_watched=True,
        _watched_path=folder_path,
        _pending_manifest_saves={},
        _wf_sync_thread=None,
        _wf_refresh_inflight=None,
        _wf_refresh_pending=False,
        _wf_refresh_generation=0,
        _manifest_state_generation=0,
        _wf_refresh_future=None,
        _watched_folder_refresh_executor=_Executor(),
        _read_watched_folder_snapshot=lambda path: path,
        _emit_watched_folder_refresh_completed=lambda *_args: None,
    )

    playlist_widget.PlaylistEditView.refresh_watched_folder(view)

    assert len(submitted) == 1
    callback, args = submitted[0]
    assert callback is view._read_watched_folder_snapshot
    assert args == (folder_path,)
    assert view._wf_refresh_inflight is not None
    assert view._wf_refresh_future is not None


def test_watched_folder_refresh_applies_only_current_snapshot():
    folder_path = "C:/linked/one"
    key = os.path.normcase(os.path.abspath(folder_path))
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, key),
        _wf_refresh_future=object(),
        _wf_refresh_superseded=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        _watched_path=folder_path,
        _is_watched=True,
        _pending_manifest_saves={},
        _wf_sync_thread=None,
        _wf_refresh_pending=False,
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        key,
        snapshot,
        None,
    )

    assert applied == [snapshot]
    assert view._wf_refresh_inflight is None
    assert view._wf_refresh_future is None


def test_availability_refresh_patches_media_without_full_qml_reset(
    monkeypatch,
):
    path = os.path.abspath("C:/linked/one/video.mp4")
    key = os.path.normcase(os.path.normpath(path))
    playlist = {
        "items": [
            {
                "id": "media-1",
                "url": path,
                "type": "video",
            }
        ]
    }
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist=playlist,
        availability=((key, True),),
        pending_files=(),
    )
    chrome_updates = []
    media_updates = []
    scheduled = []
    view = SimpleNamespace(
        _pl=playlist,
        _wf_file_availability=((key, False),),
        _watched_playlist_equivalent=lambda _playlist: True,
        _rebuild_model=lambda: None,
        _sync_playlist_chrome=lambda **kwargs: chrome_updates.append(kwargs),
        _request_missing_thumbnails=lambda: None,
        _start_wf_sync=lambda _pending: None,
        bridge=SimpleNamespace(emit_media_changed=media_updates.append),
    )
    view._availability_changed_item_ids = lambda current, availability: (
        playlist_widget.PlaylistEditView._availability_changed_item_ids(
            view,
            current,
            availability,
        )
    )
    monkeypatch.setattr(
        playlist_widget,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda delay, callback: scheduled.append((delay, callback))
        ),
    )

    playlist_widget.PlaylistEditView._apply_watched_folder_snapshot(
        view,
        snapshot,
    )

    assert chrome_updates == [{"emit_data_changed": False}]
    assert media_updates == ["media-1"]
    assert len(scheduled) == 1


def test_watched_folder_refresh_ignores_superseded_snapshot():
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(8, "current"),
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        "stale",
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_inflight == (8, "current")


def test_watched_folder_refresh_discards_immediately_superseded_snapshot():
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, "current"),
        _wf_refresh_future=object(),
        _wf_refresh_superseded=True,
        _wf_refresh_pending=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        "current",
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_superseded is False
    assert view._wf_refresh_inflight is None


def test_watched_folder_refresh_discards_snapshot_when_new_event_is_pending(
    monkeypatch,
):
    scheduled = []
    monkeypatch.setattr(
        playlist_widget,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback))),
    )
    folder_path = "C:/linked/one"
    key = os.path.normcase(os.path.abspath(folder_path))
    snapshot = playlist_widget._WatchedFolderSnapshot(
        playlist={"items": []},
        availability=(),
        pending_files=(),
    )
    applied = []
    refresh = lambda: None
    view = SimpleNamespace(
        _wf_refresh_inflight=(7, key),
        _wf_refresh_future=object(),
        _wf_refresh_pending=True,
        _wf_refresh_superseded=False,
        _wf_refresh_manifest_generation=0,
        _manifest_state_generation=0,
        refresh_watched_folder=refresh,
        _apply_watched_folder_snapshot=applied.append,
    )

    playlist_widget.PlaylistEditView._on_watched_folder_refresh_completed(
        view,
        7,
        key,
        snapshot,
        None,
    )

    assert applied == []
    assert view._wf_refresh_pending is False
    assert scheduled == [(0, refresh)]
