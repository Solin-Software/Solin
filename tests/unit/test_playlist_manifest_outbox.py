from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest.manifest import MANIFEST_FILE, ManifestWriteError
from solin.widgets.playlist.widget import PlaylistEditView


class _Timer:
    def __init__(self) -> None:
        self.interval: int | None = None

    def start(self, interval=0) -> None:
        self.interval = interval

    def stop(self) -> None:
        self.interval = None


class _ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - test executor boundary
            future.set_exception(exc)
        return future


class _ForwardSignal:
    def __init__(self, callback) -> None:
        self._callback = callback

    def emit(self, *args) -> None:
        self._callback(*args)


class _Store:
    def __init__(self) -> None:
        self.saved: list[tuple[str, dict]] = []
        self.error: BaseException | None = None

    def stage_playlist(self, folder_path: str, playlist: dict) -> None:
        pass

    def save_playlist(self, folder_path: str, playlist: dict) -> None:
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        self.saved.append((folder_path, playlist))


def _outbox(store: _Store):
    controller = type("Controller", (), {})()
    controller._pending_manifest_saves = {}
    controller._manifest_state_generation = 0
    controller._manifest_save_inflight = None
    controller._manifest_save_future = None
    controller._manifest_save_executor = _ImmediateExecutor()
    controller._manifest_save_timer = _Timer()
    controller._watched_folder_playlist_store = store
    controller._media_tree_runtime = SimpleNamespace(
        resource_lanes=ResourceLaneRegistry()
    )
    controller._arm_manifest_save_timer = (
        lambda: PlaylistEditView._arm_manifest_save_timer(controller)
    )
    controller._emit_manifest_save_completed = (
        lambda *args: PlaylistEditView._emit_manifest_save_completed(controller, *args)
    )
    controller._on_manifest_save_completed = (
        lambda *args: PlaylistEditView._on_manifest_save_completed(controller, *args)
    )
    controller._manifestSaveCompleted = _ForwardSignal(
        controller._on_manifest_save_completed
    )
    controller._warn_manifest_save_failed = lambda *_args: False
    controller._wf_refresh_inflight = None
    controller._wf_refresh_superseded = False
    controller._wf_refresh_pending = False
    controller._watched_path = ""
    return controller


def test_successful_local_publish_refreshes_to_retry_shared_outbox(monkeypatch) -> None:
    """Local durability can succeed while cloud publication remains pending."""
    from solin.widgets.playlist import widget

    controller = _outbox(_Store())
    controller._watched_path = "C:/linked/one"
    refreshed = []
    controller.refresh_watched_folder = lambda: refreshed.append(True)
    monkeypatch.setattr(widget.QTimer, "singleShot", lambda _delay, callback: callback())
    PlaylistEditView._schedule_manifest_save(
        controller, controller._watched_path, {"items": []},
    )
    next(iter(controller._pending_manifest_saves.values())).next_attempt_at = 0

    PlaylistEditView._drain_manifest_saves(controller)

    assert refreshed == [True]


def test_outbox_keeps_independent_snapshots_when_view_switches_folder() -> None:
    controller = _outbox(_Store())
    first = {"items": [{"id": "first"}]}
    second = {"items": [{"id": "second"}]}

    PlaylistEditView._schedule_manifest_save(controller, "C:/linked/one", first)
    first["items"][0]["id"] = "mutated-after-schedule"
    PlaylistEditView._schedule_manifest_save(controller, "C:/linked/two", second)

    assert len(controller._pending_manifest_saves) == 2
    snapshots = {
        request.folder_path: request.playlist
        for request in controller._pending_manifest_saves.values()
    }
    assert snapshots["C:/linked/one"]["items"][0]["id"] == "first"
    assert snapshots["C:/linked/two"]["items"][0]["id"] == "second"


def test_scheduling_save_supersedes_concurrent_folder_snapshot() -> None:
    controller = _outbox(_Store())
    controller._wf_refresh_inflight = (1, "C:/linked/one")

    PlaylistEditView._schedule_manifest_save(
        controller,
        "C:/linked/one",
        {"items": [{"id": "new-state"}]},
    )

    assert controller._manifest_state_generation == 1
    assert controller._wf_refresh_superseded is True
    assert controller._wf_refresh_pending is True


def test_outbox_coalesces_same_folder_to_latest_snapshot() -> None:
    store = _Store()
    controller = _outbox(store)

    PlaylistEditView._schedule_manifest_save(
        controller,
        "C:/linked/one",
        {"items": [{"id": "first"}]},
    )
    PlaylistEditView._schedule_manifest_save(
        controller,
        "C:/linked/one",
        {"items": [{"id": "latest"}]},
    )
    request = next(iter(controller._pending_manifest_saves.values()))
    request.next_attempt_at = 0

    PlaylistEditView._drain_manifest_saves(controller)

    assert store.saved[0][1]["items"][0]["id"] == "latest"
    assert controller._pending_manifest_saves == {}


def test_outbox_retries_transient_replace_failure_with_same_snapshot() -> None:
    store = _Store()
    cause = PermissionError("busy")
    cause.winerror = 5
    store.error = ManifestWriteError(
        Path("C:/linked/one") / MANIFEST_FILE,
        operation="replace",
        retryable=True,
        cause=cause,
    )
    controller = _outbox(store)
    PlaylistEditView._schedule_manifest_save(
        controller,
        "C:/linked/one",
        {"items": [{"id": "latest"}]},
    )
    request = next(iter(controller._pending_manifest_saves.values()))
    request.next_attempt_at = 0

    PlaylistEditView._drain_manifest_saves(controller)

    request = next(iter(controller._pending_manifest_saves.values()))
    request.next_attempt_at = 0
    PlaylistEditView._drain_manifest_saves(controller)

    assert store.saved[0][1]["items"][0]["id"] == "latest"
    assert controller._pending_manifest_saves == {}


def test_refresh_is_deferred_while_current_folder_has_pending_save() -> None:
    store = _Store()
    controller = _outbox(store)
    controller._is_watched = True
    controller._watched_path = "C:/linked/one"
    controller._wf_sync_thread = None
    controller._wf_refresh_shutdown = False
    controller._wf_refresh_pending = False
    controller._flush_image_framing_save = lambda: None
    controller._reset_watched_folder_refresh_retry = lambda: None
    store.load_playlist = lambda _path: (_ for _ in ()).throw(
        AssertionError("pending local order must not be replaced by stale manifest")
    )
    PlaylistEditView._schedule_manifest_save(
        controller,
        controller._watched_path,
        {"items": [{"id": "latest"}]},
    )

    PlaylistEditView.refresh_watched_folder(controller)

    assert controller._wf_refresh_pending is True


def test_reopening_folder_uses_pending_snapshot_instead_of_stale_disk_state() -> None:
    store = _Store()
    controller = _outbox(store)
    controller._flush_image_framing_save = lambda: None
    controller._reset_watched_folder_refresh_retry = lambda: None
    controller._watched_file_availability = lambda _playlist: ()
    controller._thumb_queue = type("Queue", (), {"clear": lambda _self: None})()
    controller._thumb_scan_timer = _Timer()
    controller._thumb_scan_items = []
    controller._id_to_thumb = {}
    controller._thumb_idx_to_id = {}
    controller._thumb_idx_to_source = {}
    controller._thumb_idx_to_intent = {}
    controller._thumb_pending_item_ids = set()
    controller._reconcile_playlist = lambda: None
    controller._start_wf_sync = lambda: None
    store.load_playlist = lambda _path: (_ for _ in ()).throw(
        AssertionError("reentry must use the pending outbox snapshot")
    )
    PlaylistEditView._schedule_manifest_save(
        controller,
        "C:/linked/one",
        {"items": [{"id": "latest-order"}]},
    )

    PlaylistEditView.load_watched_folder(controller, "C:/linked/one")

    assert controller._pl["items"][0]["id"] == "latest-order"
