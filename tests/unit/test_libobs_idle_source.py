"""Idle transactions and static-preview policy with a separate video transport fake."""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from solin.core.scenes import libobs_idle_source as module
from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
from solin.core.scenes.libobs_idle_source import IdleScreenError, LibobsIdleSource
from solin.core.scenes.libobs_idle_updates import IdleScreenUpdates
from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine


class _Source:
    def __init__(self, runtime, kind, settings):
        self.runtime = runtime
        self.kind = kind
        self.initial = dict(settings)
        self.settings = dict(settings)
        self.width = self.height = 0
        self.media_state = 1
        self.released = 0
        self.events = ["create"]
        self.observers = []
        self.pending_update = False
        self.tick_stage = 0
        self.transport = None

    def update(self, settings):
        self.events.append("update")
        self.settings.update(settings)
        self.pending_update = True

    def release(self):
        assert not self.observers
        assert self.transport is None or self.transport.closed
        assert not any(item.source is self for scene in self.runtime.scenes for item in scene.items)
        self.events.append("release")
        self.released += 1


class FakeIdleVideo:
    """Model the helper contract, never its native decoder or preload implementation."""

    def __init__(self, runtime, name, path):
        self.runtime = runtime
        self.path = path
        self.source = runtime.create_source("idle_video_scene", name, {"local_file": path})
        self.source.transport = self
        self.decoder = object()
        self.prepared = False
        self.closed = False
        self.fail_close = False
        self.live = False
        self.live_calls = []
        self.live_transitions = []
        self.error_code = ""

    def prepare(self, deadline_ms, check_current):
        self.source.update({"local_file": self.path})
        while True:
            check_current()
            if self.runtime.now_ms >= deadline_ms:
                raise module.IdleVideoError("deadline_exceeded")
            if self.path in self.runtime.bad:
                raise module.IdleVideoError("idle_media_invalid")
            if self.runtime.fail_monitor:
                raise module.IdleVideoError("idle_source_unavailable")
            if self.source.width and self.source.height and self.path not in self.runtime.no_frame:
                self.prepared = True
                return
            self.runtime.video_wait(deadline_ms)

    def set_live(self, live):
        assert self.prepared and not self.closed
        self.live_calls.append(live)
        if live != self.live:
            self.live_transitions.append(live)
            self.source.events.append(f"live:{live}")
        self.live = live

    def close(self):
        if self.closed:
            return
        if self.fail_close:
            raise RuntimeError("video cleanup failed")
        self.closed = True
        self.decoder = None
        self.source.release()


class _Item:
    def __init__(self, scene, source):
        self.scene = scene
        self.source = source
        self.removed = False
        self.released = 0

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        if self.scene.runtime.fail_transform:
            raise RuntimeError("invalid transform")
        self._bounds = value

    def remove(self):
        assert self.scene.atomic
        self.scene.items.remove(self)
        self.removed = True

    def release(self):
        assert self.removed
        self.released += 1


class _Scene:
    def __init__(self, runtime):
        self.runtime = runtime
        self.items = []
        self.all_items = []
        self.view = object()
        self.released = 0
        self.atomic = False
        self.snapshots = []

    def as_source(self):
        return self.view

    def add(self, source):
        assert self.atomic
        assert source.width and source.height
        if source.transport is not None:
            assert source.transport.prepared
        item = _Item(self, source)
        self.items.append(item)
        self.all_items.append(item)
        return item

    def release(self):
        assert not self.items
        self.released += 1

    def update(self, callback):
        self.atomic = True
        try:
            callback()
        finally:
            self.atomic = False
            # Rendering only observes the final transaction, never both children.
            self.snapshots.append(tuple(item.source for item in self.items))


class _Runtime:
    def __init__(self):
        self.scenes = []
        self.sources = []
        self.video = SimpleNamespace(width=1920, height=1080)
        self.now_ms = 1000
        self.bad = set()
        self.no_frame = set()
        self.no_update = set()
        self.fail_transform = False
        self.fail_create = False
        self.fail_monitor = False
        self.on_wait = None
        self.on_update = None
        self.ob = SimpleNamespace(
            Scene=SimpleNamespace(create_private=self.create_scene),
            Source=SimpleNamespace(create_private=self.create_source),
            BoundsType=SimpleNamespace(SCALE_INNER=2),
        )

    def create_scene(self, _name):
        scene = _Scene(self)
        self.scenes.append(scene)
        return scene

    def create_source(self, kind, _name, settings):
        if self.fail_create:
            raise RuntimeError("source plugin unavailable")
        source = _Source(self, kind, settings)
        self.sources.append(source)
        return source

    @staticmethod
    def atomic_scene_update(scene, callback):
        scene.update(callback)

    def observe_source_updates(self, source, callback):
        source.observers.append(callback)

        def disconnect():
            source.events.append("disconnect")
            source.observers.remove(callback)

        return disconnect

    def tick(self, _deadline):
        self.now_ms += 10
        if self.on_wait:
            callback, self.on_wait = self.on_wait, None
            callback()
        for source in self.sources:
            if source.released:
                continue
            path = source.settings.get("file") or source.settings.get("local_file")
            if source.pending_update:
                if path in self.no_update:
                    continue
                source.pending_update = False
                source.tick_stage = 1
                for callback in tuple(source.observers):
                    callback()
                if self.on_update:
                    self.on_update(source)
            elif source.tick_stage == 1:
                source.tick_stage = 2
                if path in self.bad:
                    source.media_state = 7
                    continue
                source.width, source.height = (
                    (self.video.width, self.video.height)
                    if source.transport is not None else (320, 240)
                )


@pytest.fixture
def provider(monkeypatch):
    runtime = _Runtime()
    owner = LibobsIdleSource(runtime)
    monkeypatch.setattr(module.time, "monotonic", lambda: runtime.now_ms / 1000)
    monkeypatch.setattr(module, "LibobsIdleVideo", FakeIdleVideo)
    monkeypatch.setattr(owner, "_wait", runtime.tick)
    runtime.video_wait = lambda deadline: owner._wait(deadline)
    yield owner, runtime
    owner.close()


def _file(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"native decoder is replaced by a deterministic test double")
    return str(path)


def _state(tmp_path, revision=1, *, media="video.mp4", fallback="year.png", text_revision=1):
    return IdleScreenState(
        revision,
        _file(tmp_path, media) if media else "",
        _file(tmp_path, fallback) if fallback else "",
        text_revision,
    )


def test_constructor_and_demand_do_not_allocate_native_resources(provider):
    owner, runtime = provider
    owner.set_demand(True)
    owner.set_demand(False)
    assert runtime.scenes == runtime.sources == []
    assert owner.state == IdleScreenState()


def test_source_is_one_borrowed_stable_canvas_and_empty_apply_is_transparent(provider):
    owner, runtime = provider
    source = owner.source
    owner.apply(IdleScreenState(), 2000)
    assert owner.source is source
    assert len(runtime.scenes) == 1
    assert runtime.sources == []
    assert runtime.scenes[0].items == []
    assert owner.error_code == ""


@pytest.mark.parametrize("media", ["photo.PNG", "movie.mp4", ""])
def test_first_native_frame_or_image_update_precedes_atomic_full_canvas_contain(
    provider,
    tmp_path,
    media,
):
    owner, runtime = provider
    state = _state(tmp_path, media=media)

    def verify_not_published(_source):
        assert not runtime.scenes[0].items

    runtime.on_update = verify_not_published
    owner.apply(state, 2000)
    assert runtime.now_ms == 1020  # update ack alone is insufficient
    assert owner.state == state
    assert len(runtime.sources) == 1
    item = runtime.scenes[0].items[0]
    assert item.pos == (0, 0)
    assert item.alignment == 5
    assert item.bounds_type == 2
    assert item.bounds_alignment == 0
    assert item.bounds == (1920, 1080)
    assert len(runtime.scenes[0].snapshots[-1]) == 1


def test_video_preparation_is_delegated_before_publication(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    presentation = runtime.sources[0]
    video = presentation.transport
    assert video.prepared and not video.closed
    assert video.live_calls == [False]
    assert presentation.kind == "idle_video_scene"
    assert (presentation.width, presentation.height) == (1920, 1080)
    assert runtime.scenes[0].items[0].source is video.source


def test_preview_demand_keeps_video_static_and_preserves_one_shared_presentation(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    video = runtime.sources[0].transport
    for _ in range(50):
        owner.set_demand(True)
        assert owner.source is runtime.scenes[0].view
    owner.set_demand(False)
    owner.set_demand(True)
    assert not video.live
    assert video.live_transitions == []
    assert all(live is False for live in video.live_calls)
    assert len(runtime.sources) == len(runtime.scenes) == 1


def test_apply_with_preview_only_publishes_a_static_video(provider, tmp_path):
    owner, runtime = provider
    owner.set_demand(True)
    owner.apply(_state(tmp_path), 2000)
    assert runtime.sources[0].transport.live_calls == [False]


def test_first_live_entry_plays_and_last_live_exit_restores_static_preview(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    video = runtime.sources[0].transport
    owner.set_demand(True)
    assert video.live_transitions == []
    owner.set_demand(True, live=True)
    assert video.live_transitions == [True]
    owner.set_demand(True, live=True)
    assert video.live_transitions == [True]
    owner.set_demand(True)
    assert video.live_transitions == [True, False]
    assert not video.live and owner._demand and not owner._live_demand
    owner.set_demand(True, live=True)
    owner.set_demand(False)
    owner.set_demand(True, live=True)
    assert video.live_transitions == [True, False, True, False, True]


def test_unchanged_live_demand_still_reconciles_an_async_prepared_decoder(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    video = runtime.sources[0].transport
    owner.set_demand(True, live=True)
    calls = len(video.live_calls)
    owner.set_demand(True, live=True)
    assert len(video.live_calls) == calls + 1
    assert video.live_calls[-1] is True
    assert video.live_transitions == [True]


def test_background_video_error_is_exposed_without_changing_confirmed_idle_state(provider, tmp_path):
    owner, runtime = provider
    state = _state(tmp_path)
    owner.apply(state, 2000)
    video = runtime.sources[0].transport
    video.error_code = "idle_media_unavailable"
    assert owner.error_code == "idle_media_unavailable"
    assert owner.state == state
    assert runtime.scenes[0].items[0].source is video.source
    video.error_code = ""
    assert owner.error_code == ""


def test_live_without_any_consumer_normalizes_to_static_video(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    owner.set_demand(False, live=True)
    video = runtime.sources[0].transport
    assert not video.live
    assert video.live_transitions == []
    assert not owner._demand and not owner._live_demand


def test_live_edge_deferred_by_control_lock_reconciles_once_on_next_render(provider, tmp_path):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    owner.set_demand(True)
    video = runtime.sources[0].transport
    done = threading.Event()
    with owner._lock:
        worker = threading.Thread(
            target=lambda: (owner.set_demand(True, live=True), done.set()),
        )
        worker.start()
        assert done.wait(1), "Live-demand callback blocked on the control lock"
        assert not video.live
    worker.join(timeout=1)
    owner.set_demand(True, live=True)
    owner.set_demand(True, live=True)
    assert video.live_transitions == [True]


def test_apply_already_live_uses_current_live_demand_at_publication(provider, tmp_path):
    owner, runtime = provider
    owner.set_demand(True, live=True)
    owner.apply(_state(tmp_path), 2000)
    video = runtime.sources[0].transport
    assert video.live_calls == [True]
    owner.set_demand(True, live=True)
    assert video.live_transitions == [True]


def test_demand_callback_returns_without_waiting_for_control_lock(provider):
    owner, _runtime = provider
    done = threading.Event()
    with owner._lock:
        worker = threading.Thread(target=lambda: (owner.set_demand(True), done.set()))
        worker.start()
        assert done.wait(1), "OBS render callback blocked on the control lock"
    worker.join(timeout=1)
    owner.set_demand(True)
    assert owner._demand


@pytest.mark.parametrize("live", [False, True])
def test_demand_changed_during_commit_reconciles_on_next_render(
    provider, tmp_path, monkeypatch, live,
):
    owner, runtime = provider

    def atomic(scene, update):
        worker = threading.Thread(target=lambda: owner.set_demand(True, live=live))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        scene.update(update)

    monkeypatch.setattr(runtime, "atomic_scene_update", atomic)
    owner.apply(_state(tmp_path), 2000)
    video = runtime.sources[0].transport
    assert video.live_calls == [False]
    owner.set_demand(True, live=live)
    assert video.live is live
    assert video.live_transitions == ([True] if live else [])


def test_fallback_changes_during_video_do_not_allocate_or_reset_transport(provider, tmp_path):
    owner, runtime = provider
    state = _state(tmp_path)
    owner.apply(state, 2000)
    owner.set_demand(True, live=True)
    video = runtime.sources[0]
    events = list(video.events)
    updated = IdleScreenState(2, state.media_path, _file(tmp_path, "new-year.png"), 2)
    owner.apply(updated, 2000)
    owner.apply(updated, 2000)
    assert video.events == events
    assert len(runtime.sources) == 1
    assert owner.state == updated
    owner.apply(IdleScreenState(3, "", updated.yeartext_image_path, 2), 2000)
    assert video.released == 1
    assert runtime.sources[1].settings["file"] == updated.yeartext_image_path
    assert runtime.scenes[0].items[0].source is runtime.sources[1]


def test_same_png_new_text_revision_reloads_native_image_and_same_revision_is_noop(
    provider, tmp_path
):
    owner, runtime = provider
    first = _state(tmp_path, media="")
    owner.apply(first, 2000)
    owner.apply(first, 0)  # already committed, no native work needs a deadline
    assert len(runtime.sources) == 1
    owner.apply(IdleScreenState(2, "", first.yeartext_image_path, 2), 2000)
    assert len(runtime.sources) == 2
    assert runtime.sources[0].released == 1


@pytest.mark.parametrize(
    "failure", ["missing", "extension", "decode", "frame", "update", "create", "monitor"]
)
def test_failed_replacement_keeps_committed_state_and_child_and_releases_candidate(
    provider,
    tmp_path,
    failure,
):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    scene_source = owner.source
    old_item = runtime.scenes[0].items[0]
    bad_path = _file(tmp_path, "invalid.mp3" if failure == "extension" else "invalid.mp4")
    expected = {
        "missing": "idle_media_unavailable",
        "extension": "idle_media_invalid",
        "decode": "idle_media_invalid",
        "frame": "deadline_exceeded",
        "update": "deadline_exceeded",
        "create": "idle_source_unavailable",
        "monitor": "idle_source_unavailable",
    }[failure]
    if failure == "missing":
        Path(bad_path).unlink()
    if failure == "decode":
        runtime.bad.add(bad_path)
    if failure == "frame":
        runtime.no_frame.add(bad_path)
    if failure == "update":
        runtime.no_update.add(bad_path)
    runtime.fail_create = failure == "create"
    runtime.fail_monitor = failure == "monitor"
    with pytest.raises(IdleScreenError) as error:
        owner.apply(IdleScreenState(2, bad_path), 1100)
    assert error.value.error_code == expected
    assert owner.state == initial
    assert owner.source is scene_source
    assert runtime.scenes[0].items == [old_item]
    assert runtime.sources[0].released == 0
    assert all(source.released == 1 for source in runtime.sources[1:])


@pytest.mark.parametrize("problem", ["bad", "no_update"])
def test_image_requires_native_ack_and_nonzero_dimensions_and_times_out_without_publication(
    provider,
    tmp_path,
    problem,
):
    owner, runtime = provider
    state = _state(tmp_path, media="photo.png")
    getattr(runtime, problem).add(state.media_path)
    with pytest.raises(IdleScreenError, match="deadline_exceeded"):
        owner.apply(state, 1100)
    assert owner.state == IdleScreenState()
    assert not runtime.scenes[0].items
    assert runtime.sources[0].released == 1
    assert runtime.sources[0].events[-2:] == ["disconnect", "release"]


def test_atomic_transform_failure_rolls_back_before_any_render(provider, tmp_path):
    owner, runtime = provider
    first = _state(tmp_path)
    owner.apply(first, 2000)
    old = runtime.sources[0]
    runtime.fail_transform = True
    with pytest.raises(IdleScreenError, match="idle_source_unavailable"):
        owner.apply(_state(tmp_path, 2, media="next.mp4"), 2000)
    assert owner.state == first
    assert runtime.scenes[0].snapshots == [(old,), (old,)]
    assert runtime.sources[-1].released == 1
    assert runtime.scenes[0].all_items[-1].released == 1


@pytest.mark.parametrize("fallback", ["year.png", ""])
def test_recovery_commits_requested_configuration_and_fallback_then_reports_error(
    provider,
    tmp_path,
    fallback,
):
    owner, runtime = provider
    state = _state(tmp_path, fallback=fallback)
    Path(state.media_path).unlink()
    with pytest.raises(IdleScreenError, match="idle_media_unavailable"):
        owner.apply(state, 2000, recover=True)
    assert owner.state == state
    assert owner.error_code == "idle_media_unavailable"
    assert len(runtime.sources) == bool(fallback)
    assert bool(runtime.scenes[0].items) == bool(fallback)
    if fallback:
        assert runtime.sources[0].settings["file"] == state.yeartext_image_path
    Path(state.media_path).write_bytes(b"recovered media")
    owner.apply(state, 2000)
    assert owner.state == state
    assert owner.error_code == ""
    assert runtime.scenes[0].items[0].source.kind == "idle_video_scene"


def test_recovery_when_deadline_expires_clears_to_transparent_without_extending_deadline(
    provider,
    tmp_path,
):
    owner, runtime = provider
    first = _state(tmp_path)
    owner.apply(first, 2000)
    requested = _state(tmp_path, 2, media="silent.mp4")
    runtime.no_frame.add(requested.media_path)
    with pytest.raises(IdleScreenError, match="deadline_exceeded"):
        owner.apply(requested, 1100, recover=True)
    assert runtime.now_ms == 1100
    assert owner.state == requested
    assert owner.error_code == "deadline_exceeded"
    assert not runtime.scenes[0].items
    assert all(source.released == 1 for source in runtime.sources)


def test_unavailable_fallback_during_recovery_is_transparent(provider, tmp_path):
    owner, runtime = provider
    requested = IdleScreenState(1, str(tmp_path / "lost.mp4"), str(tmp_path / "lost.png"), 1)
    with pytest.raises(IdleScreenError, match="idle_media_unavailable"):
        owner.apply(requested, 2000, recover=True)
    assert owner.state == requested
    assert not runtime.scenes[0].items
    assert runtime.sources == []


def test_stale_revision_and_equal_revision_conflicts_cannot_change_current_source(
    provider, tmp_path
):
    owner, runtime = provider
    current = _state(tmp_path, 4)
    owner.apply(current, 2000)
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(IdleScreenState(3), 2000, recover=True)
    with pytest.raises(IdleScreenError, match="revision_conflict"):
        owner.apply(IdleScreenState(4), 2000, recover=True)
    assert owner.state == current
    assert len(runtime.sources) == 1


def test_superseded_preparation_closes_video_helper_and_cannot_replace_newer_state(
    provider,
    tmp_path,
):
    owner, runtime = provider
    older = _state(tmp_path, 1)
    newer = _state(tmp_path, 2, media="new.mp4")
    runtime.on_wait = lambda: owner.apply(newer, 2000)
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(older, 2000, recover=True)
    assert owner.state == newer
    assert runtime.sources[0].released == 1
    assert runtime.sources[0].transport.closed
    assert runtime.scenes[0].items[0].source is runtime.sources[1]


def test_failed_newer_request_cannot_be_overwritten_by_delayed_older_request(provider, tmp_path):
    owner, runtime = provider
    with pytest.raises(IdleScreenError, match="idle_media_unavailable"):
        owner.apply(IdleScreenState(3, str(tmp_path / "missing.mp4")), 2000)
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(_state(tmp_path, 2), 2000)
    assert not runtime.sources


def test_admission_is_lazy_and_rejects_delayed_lower_or_conflicting_apply(provider):
    owner, runtime = provider
    latest = IdleScreenState(3)
    owner.admit(latest)
    owner.admit(latest)
    assert owner.state == IdleScreenState()
    assert owner.error_code == ""
    assert runtime.sources == runtime.scenes == []
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(IdleScreenState(2), 2000, recover=True)
    with pytest.raises(IdleScreenError, match="revision_conflict"):
        owner.apply(IdleScreenState(3, "conflict.mp4"), 2000, recover=True)
    assert runtime.sources == runtime.scenes == []
    owner.apply(latest, 2000)
    assert owner.state == latest
    assert runtime.sources == []
    owner.close()
    with pytest.raises(IdleScreenError, match="closed"):
        owner.admit(latest)


def test_identical_admission_during_preparation_keeps_candidate_and_transport(provider, tmp_path):
    owner, runtime = provider
    state = _state(tmp_path)
    owner.admit(state)
    runtime.on_wait = lambda: owner.admit(state)
    owner.apply(state, 2000)
    assert owner.state == state
    assert len(runtime.sources) == 1
    source = runtime.sources[0]
    events = list(source.events)
    owner.admit(state)
    owner.apply(state, 0)
    assert source.events == events
    assert not owner._wake.is_set()


def test_newer_admission_cancels_candidate_without_changing_committed_content(provider, tmp_path):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    owner.set_demand(True, live=True)
    old = runtime.sources[0]
    events = list(old.events)
    preparing = _state(tmp_path, 2, media="obsolete.mp4")
    latest = _state(tmp_path, 3, media="latest.mp4")
    runtime.on_wait = lambda: owner.admit(latest)
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(preparing, 2000, recover=True)
    assert owner.state == initial
    assert owner.error_code == ""
    assert old.events == events
    assert runtime.sources[-1].transport.closed
    assert runtime.sources[-1].released == 1
    assert runtime.scenes[0].items[0].source is old
    # Even the cached successful-state shortcut must respect newer admission.
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(initial, 0)
    owner.apply(latest, 2000)
    assert owner.state == latest
    assert old.released == 1


@pytest.mark.parametrize("rejected", ["stale", "conflicting"])
def test_rejected_admission_cannot_cancel_a_useful_candidate(provider, tmp_path, rejected):
    owner, runtime = provider
    state = _state(tmp_path, 3)
    invalid = IdleScreenState(2 if rejected == "stale" else 3)

    def admit_invalid():
        expected = "stale_revision" if rejected == "stale" else "revision_conflict"
        with pytest.raises(IdleScreenError, match=expected):
            owner.admit(invalid)

    runtime.on_wait = admit_invalid
    owner.apply(state, 2000)
    assert owner.state == state
    assert len(runtime.sources) == 1
    assert runtime.sources[0].released == 0


def test_admitted_recovery_retains_request_and_identical_admission_allows_retry(provider, tmp_path):
    owner, runtime = provider
    requested = _state(tmp_path)
    Path(requested.media_path).unlink()
    owner.admit(requested)
    with pytest.raises(IdleScreenError, match="idle_media_unavailable"):
        owner.apply(requested, 2000, recover=True)
    assert owner.state == requested
    assert owner.error_code == "idle_media_unavailable"
    fallback = runtime.sources[0]
    owner.admit(requested)
    assert owner.error_code == "idle_media_unavailable"
    Path(requested.media_path).write_bytes(b"recovered media")
    owner.apply(requested, 2000)
    assert owner.state == requested
    assert owner.error_code == ""
    assert fallback.released == 1
    assert runtime.scenes[0].items[0].source.kind == "idle_video_scene"


def test_cancel_preparation_preserves_committed_source_state_and_live_transport(provider, tmp_path):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    owner.set_demand(True, live=True)
    old = runtime.sources[0]
    events = list(old.events)
    runtime.on_wait = owner.cancel_preparation
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(_state(tmp_path, 2, media="canceled.mp4"), 2000, recover=True)
    assert owner.state == initial
    assert owner.error_code == ""
    assert runtime.scenes[0].items[0].source is old
    assert old.events == events
    assert owner._demand and owner._live_demand
    assert runtime.sources[-1].transport.closed
    assert runtime.sources[-1].released == 1
    assert not owner._wake.is_set()
    # Cancellation does not poison waiting for the next queued candidate.
    latest = _state(tmp_path, 3, media="latest.mp4")
    owner.apply(latest, 2000)
    assert owner.state == latest
    assert old.released == 1


def test_cancel_without_pending_candidate_is_a_committed_presentation_noop(provider, tmp_path):
    owner, runtime = provider
    state = _state(tmp_path)
    owner.apply(state, 2000)
    events = list(runtime.sources[0].events)
    owner.cancel_preparation()
    owner.apply(state, 0)
    assert owner.state == state
    assert runtime.sources[0].events == events
    assert not owner._wake.is_set()


def test_cancelled_admission_requires_explicit_readmission_for_same_revision_retry(provider, tmp_path):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    pending = _state(tmp_path, 2, media="retry.mp4")
    owner.admit(pending)
    owner.cancel_preparation()
    for recover in (False, True):
        with pytest.raises(IdleScreenError, match="stale_revision"):
            owner.apply(pending, 2000, recover=recover)
    assert owner.state == initial
    assert len(runtime.sources) == 1
    owner.admit(pending)
    owner.apply(pending, 2000)
    assert owner.state == pending
    assert len(runtime.sources) == 2
    assert runtime.sources[0].released == 1


def test_cancelling_recovery_does_not_erase_state_and_readmission_retries_same_choice(
    provider, tmp_path
):
    owner, runtime = provider
    state = _state(tmp_path)
    Path(state.media_path).unlink()
    owner.admit(state)
    with pytest.raises(IdleScreenError, match="idle_media_unavailable"):
        owner.apply(state, 2000, recover=True)
    owner.cancel_preparation()
    with pytest.raises(IdleScreenError, match="stale_revision"):
        owner.apply(state, 2000, recover=True)
    assert owner.state == state
    assert owner.error_code == "idle_media_unavailable"
    assert len(runtime.sources) == 1
    Path(state.media_path).write_bytes(b"recovered")
    owner.admit(state)
    owner.apply(state, 2000)
    assert owner.state == state
    assert owner.error_code == ""
    assert runtime.sources[0].released == 1


@pytest.mark.parametrize("foreign_context", [None, "session_id", "process_generation"])
def test_worker_admits_latest_before_delayed_handler_can_begin_apply(
    provider, tmp_path, foreign_context
):
    """B arrives after dispatch of A, before A enters the native provider."""
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    owner.set_demand(True, live=True)
    old = runtime.sources[0]
    old_events = list(old.events)
    a = _state(tmp_path, 2, media="obsolete.mp4")
    b = _state(tmp_path, 3, media="latest.mp4")
    entered = {2: threading.Event(), 3: threading.Event()}
    release = {2: threading.Event(), 3: threading.Event()}
    condition = threading.Condition()
    responses = {}
    engine = LibobsSidecarEngine(runtime_factory=None)
    engine._runtime_started = True
    engine._session_id = "idle-test"
    engine._process_generation = "generation-1"
    engine._idle_source = owner

    def request(state):
        return SceneIpcEnvelope(
            message_type="set_idle_screen",
            request_id=f"idle-{state.revision}",
            session_id="idle-test",
            process_generation="generation-1",
            sequence=state.revision,
            document_revision=1,
            deadline_monotonic_ms=2000,
            payload={"idle_screen": state.to_record()},
        )

    def handle(envelope):
        state = IdleScreenState.from_record(envelope.payload["idle_screen"])
        entered[state.revision].set()
        assert release[state.revision].wait(3), "test handler was not released"
        try:
            owner.apply(state, envelope.deadline_monotonic_ms)
            payload = {"applied": True}
        except IdleScreenError as error:
            payload = {"applied": False, "error_code": error.error_code}
        return replace(envelope, message_type="ack", payload=payload)

    def emit(envelope):
        with condition:
            responses[envelope.request_id] = envelope
            condition.notify_all()

    updates = IdleScreenUpdates(
        handle, emit, owner.cancel_preparation, admit=engine.admit_idle_screen
    )
    try:
        updates.submit(request(a))
        assert entered[2].wait(3)
        if foreign_context:
            foreign = replace(
                request(IdleScreenState(100)),
                **{foreign_context: "obsolete-context"},
            )
            updates.submit(foreign)
            assert responses[foreign.request_id].payload["error_code"] == "session_mismatch"
            assert owner._requested == a
            assert owner.state == initial
        updates.submit(request(b))
        release[2].set()
        assert entered[3].wait(3)
        assert owner.state == initial, "obsolete A was visibly committed before B started"
        assert runtime.sources == [old], "obsolete A must not even open its decoder"
        assert old.events == old_events
        release[3].set()
        with condition:
            assert condition.wait_for(lambda: "idle-3" in responses, timeout=3)
        assert responses["idle-2"].payload["error_code"] == "stale_revision"
        assert responses["idle-3"].payload["applied"] is True
        assert owner.state == b
        assert all(
            source.settings.get("local_file") != a.media_path
            for snapshot in runtime.scenes[0].snapshots
            for source in snapshot
        )
    finally:
        for event in release.values():
            event.set()
        updates.close()


def test_cancel_drains_pending_wait_promptly_before_shutdown(provider, tmp_path, monkeypatch):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    entered = threading.Event()
    errors = []

    def wait(_deadline):
        entered.set()
        owner._wake.wait(1 / 120)

    def prepare():
        try:
            owner.apply(_state(tmp_path, 2, media="pending.mp4"), 2000)
        except IdleScreenError as error:
            errors.append(error.error_code)

    monkeypatch.setattr(runtime, "video_wait", wait)
    worker = threading.Thread(target=prepare)
    worker.start()
    assert entered.wait(1)
    owner.cancel_preparation()
    worker.join(timeout=1)
    assert not worker.is_alive(), "Canceled preparation did not leave its bounded wait"
    assert errors == ["stale_revision"]
    assert owner.state == initial
    assert runtime.sources[-1].transport.closed
    assert runtime.sources[-1].released == 1
    assert not owner._wake.is_set()
    owner.close()
    assert all(source.released == 1 for source in runtime.sources)
    assert runtime.scenes[0].released == 1


def test_worker_close_invalidates_admission_before_dispatched_handler_enters_apply(
    provider, tmp_path
):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    pending = _state(tmp_path, 2, media="shutdown-obsolete.mp4")
    engine = LibobsSidecarEngine(runtime_factory=None)
    engine._runtime_started = True
    engine._session_id = "idle-test"
    engine._process_generation = "generation-1"
    engine._idle_source = owner
    request = SceneIpcEnvelope(
        message_type="set_idle_screen", request_id="closing-idle",
        session_id="idle-test", process_generation="generation-1", sequence=2,
        document_revision=1, deadline_monotonic_ms=2000,
        payload={"idle_screen": pending.to_record()},
    )
    entered, release, cancelled = (threading.Event() for _ in range(3))
    replies = []

    def handle(envelope):
        entered.set()
        assert release.wait(3), "test handler was not released"
        return engine.handle(envelope)

    def cancel():
        owner.cancel_preparation()
        cancelled.set()

    updates = IdleScreenUpdates(handle, replies.append, cancel, admit=engine.admit_idle_screen)
    closing = threading.Thread(target=updates.close)
    try:
        updates.submit(request)
        assert entered.wait(3)
        closing.start()
        assert cancelled.wait(3)
        release.set()
        closing.join(3)
        assert not closing.is_alive(), "shutdown must drain the delayed handler promptly"
        assert replies[0].payload["error_code"] == "closed"
        assert owner.state == initial, "a dispatched handler reopened cancelled admission"
        assert len(runtime.sources) == 1, "shutdown must not open another decoder"
        assert runtime.sources[0].released == 0
        assert not owner._wake.is_set()
    finally:
        release.set()
        if closing.ident is not None:
            closing.join(3)
        updates.close()


def test_close_detaches_item_then_releases_source_and_private_scene_and_is_idempotent(
    provider,
    tmp_path,
):
    owner, runtime = provider
    owner.apply(_state(tmp_path), 2000)
    owner.close()
    owner.close()
    owner.set_demand(True)
    assert runtime.sources[0].released == 1
    assert runtime.scenes[0].all_items[0].released == 1
    assert runtime.scenes[0].released == 1
    with pytest.raises(IdleScreenError, match="closed"):
        _ = owner.source
    with pytest.raises(IdleScreenError, match="closed"):
        owner.apply(IdleScreenState(2), 2000)


def test_failed_retired_video_cleanup_retains_helper_until_cleanup_retry(provider, tmp_path):
    owner, runtime = provider
    state = _state(tmp_path)
    owner.apply(state, 2000)
    source = runtime.sources[0]
    source.transport.fail_close = True
    owner.apply(_state(tmp_path, 2, media="replacement.mp4"), 2000)
    assert owner.state.revision == 2
    assert source.released == 0
    assert not source.transport.closed
    source.transport.fail_close = False
    owner.close()
    assert source.transport.closed
    assert source.released == 1


def test_failed_unprepared_video_cleanup_retains_helper_without_committing_choice(provider, tmp_path):
    owner, runtime = provider
    initial = _state(tmp_path)
    owner.apply(initial, 2000)
    committed = runtime.sources[0]
    replacement = _state(tmp_path, 2, media="unprepared.mp4")
    runtime.no_frame.add(replacement.media_path)

    def fail_candidate_close(source):
        if source.settings.get("local_file") == replacement.media_path:
            source.transport.fail_close = True

    runtime.on_update = fail_candidate_close
    with pytest.raises(IdleScreenError, match="deadline_exceeded"):
        owner.apply(replacement, 1100)
    candidate = runtime.sources[1]
    assert owner.state == initial
    assert runtime.scenes[0].items[0].source is committed
    assert not candidate.transport.prepared and not candidate.transport.closed
    assert candidate.released == 0 and committed.released == 0
    owner.close()
    assert committed.released == 1 and candidate.released == 0
    candidate.transport.fail_close = False
    owner.close()
    owner.close()
    assert candidate.transport.closed and candidate.released == 1
    assert runtime.scenes[0].released == 1


def test_shutdown_cancels_and_drains_preparation_before_releasing_private_scene(
    provider,
    tmp_path,
    monkeypatch,
):
    owner, runtime = provider
    entered = threading.Event()
    errors = []

    def wait(_deadline):
        entered.set()
        assert owner._wake.wait(2)

    def prepare():
        try:
            owner.apply(_state(tmp_path), 2000)
        except IdleScreenError as error:
            errors.append(error.error_code)

    monkeypatch.setattr(runtime, "video_wait", wait)
    preparing = threading.Thread(target=prepare)
    preparing.start()
    assert entered.wait(2)
    closing = threading.Thread(target=owner.close)
    closing.start()
    preparing.join(timeout=2)
    closing.join(timeout=2)
    assert not preparing.is_alive() and not closing.is_alive()
    assert errors == ["closed"]
    assert runtime.sources[0].transport.closed
    assert runtime.sources[0].released == 1
    assert runtime.scenes[0].released == 1
    assert not runtime.scenes[0].items


def test_provider_import_does_not_import_qt_in_clean_python_process():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from solin.core.scenes.libobs_idle_source import LibobsIdleSource; "
                "assert not any(name.startswith('PySide6') for name in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
