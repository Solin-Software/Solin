"""Effective idle demand across native transitions, previews and display routes."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
from solin.core.scenes.process_engine import _source_health_from_envelope


class _Tree:
    def __init__(self, *pointers):
        self.pointers = pointers

    def enum_active_tree(self):
        pytest.fail("Demand must not use owning source enumeration")


class _Idle:
    def __init__(self):
        self.demand = False
        self.restarts = 0
        self.pauses = 0
        self.live = False
        self.transport_error_code = ""

    @property
    def source(self):
        pytest.fail("Render demand must never acquire the idle owner's source lock")

    def set_demand(self, demanded, *, live=False):
        if demanded and (not self.demand or (live and not self.live)):
            self.restarts += 1
        self.pauses += self.demand and not demanded
        self.demand = demanded
        self.live = live


def _engine():
    idle, pointer = _Idle(), object()
    program, projection, editor = _Tree(), _Tree(), _Tree()
    engine = LibobsSidecarEngine()
    engine._idle_source = idle
    engine._runtime = SimpleNamespace(
        active_source_tree_contains=lambda source, target: (
            source is not None and target._ptr in source.pointers
        ),
        transition_source_tree_contains=lambda source, target: (
            source is not None and target._ptr in source.pointers
        ),
    )
    engine._idle_scene_source = SimpleNamespace(_ptr=pointer)
    engine._idle_definitions = ("idle-definition",)
    engine._scene_graph = SimpleNamespace(
        program_source=program,
        editor_scene_source=lambda scene_id: editor if scene_id == "editor" else _Tree(),
        scene_uses_idle_source=lambda scene_id: scene_id in {"idle", "nested-idle"},
    )
    engine._projection_route = SimpleNamespace(transition_source=projection)
    engine._virtual_camera = SimpleNamespace(active=False)
    engine._recorder = SimpleNamespace(active=False)
    engine._program_egress = SimpleNamespace(active=False)
    engine._preview_egress = SimpleNamespace(active=False)
    engine._editor_scene_id = "editor"
    engine._window_output = SimpleNamespace(
        render_targets=(), hydrate_lock=threading.RLock(),
    )
    return engine, idle, pointer, (program, projection, editor)


@pytest.mark.parametrize("bus,index", [("virtual_camera", 0), ("media_windows", 1), ("editor", 2)])
def test_native_demand_requires_a_consumer_and_traverses_the_active_tree(bus, index):
    engine, idle, pointer, trees = _engine()
    tree = trees[index]
    tree.pointers = (object(), pointer, object())
    engine._reconcile_idle_demand()
    assert not idle.demand
    if bus == "virtual_camera":
        engine._virtual_camera.active = True
    elif bus == "media_windows":
        engine._window_output.render_targets = (("projection", ""),)
    else:
        engine._preview_egress.active = True
    engine._reconcile_idle_demand()
    assert idle.demand and idle.restarts == 1
    engine._virtual_camera.active = False
    engine._window_output.render_targets = ()
    engine._preview_egress.active = False
    engine._reconcile_idle_demand()
    assert not idle.demand and idle.pauses == 1


def _health_engine():
    engine, idle, _, _ = _engine()
    engine._session_id = "idle-session"
    engine._process_generation = "idle-generation"
    engine._document_revision = 19
    engine._idle_definitions = ("idle-one", "idle-two")
    events = []
    engine.set_event_sink(events.append)
    return engine, idle, events


def test_reset_failure_reports_typed_health_once_per_source_with_current_context():
    engine, idle, events = _health_engine()
    engine._reconcile_idle_demand()
    assert events == []
    idle.transport_error_code = "idle_media_unavailable"
    for _ in range(5):
        engine._reconcile_idle_demand()
    assert len(events) == 2
    for event, source_id in zip(events, engine._idle_definitions, strict=True):
        assert event.message_type == "source_health"
        assert event.session_id == "idle-session"
        assert event.process_generation == "idle-generation"
        assert event.document_revision == 19
        health = _source_health_from_envelope(event)
        assert health.source_id == source_id
        assert health.status == "failed"
        assert health.error_code == "idle_media_unavailable"
        assert health.message == "Idle video could not restart; showing its initial frame"


def test_reset_health_is_republished_for_the_current_profile_and_generation():
    engine, idle, events = _health_engine()
    idle.transport_error_code = "idle_source_unavailable"
    engine._reconcile_idle_demand()
    engine._process_generation = "next-generation"
    engine._document_revision = 0
    engine._idle_definitions = ("new-idle",)
    engine._reconcile_idle_demand()
    engine._reconcile_idle_demand()
    assert len(events) == 3
    assert events[-1].process_generation == "next-generation"
    assert events[-1].document_revision == 0
    assert _source_health_from_envelope(events[-1]).source_id == "new-idle"


def test_reset_notification_waits_for_a_sink_and_a_committed_document():
    engine, idle, events = _health_engine()
    idle.transport_error_code = "idle_media_unavailable"
    engine.set_event_sink(None)
    engine._reconcile_idle_demand()
    engine.set_event_sink(events.append)
    engine._document_revision = None
    engine._reconcile_idle_demand()
    assert events == []
    engine._document_revision = 19
    engine._reconcile_idle_demand()
    assert len(events) == 2


def test_transport_recovery_clears_health_once():
    engine, idle, events = _health_engine()
    idle.transport_error_code = "idle_media_unavailable"
    engine._reconcile_idle_demand()
    idle.transport_error_code = ""
    for _ in range(5):
        engine._reconcile_idle_demand()
    assert len(events) == 4
    assert all(event.payload == {
        "source_id": source_id, "status": "ready", "error_code": "", "message": "",
    } for event, source_id in zip(events[2:], engine._idle_definitions, strict=True))


@pytest.mark.parametrize("error_code", ["", "idle_media_unavailable"])
def test_control_health_replaces_transport_health_without_duplicate_or_false_recovery(error_code):
    engine, idle, events = _health_engine()
    idle.transport_error_code = "idle_media_unavailable"
    engine._reconcile_idle_demand()
    # A replacement clears the transport failure. Hydrate may instead restore
    # annual text while retaining the selected video's recovery failure.
    idle.transport_error_code = ""
    request = SceneIpcEnvelope(
        message_type="hydrate", request_id="hydrate-new", session_id="idle-session",
        process_generation="idle-generation", sequence=21, document_revision=19,
        deadline_monotonic_ms=1000, payload={},
    )
    engine._publish_idle_health(request, error_code)
    for _ in range(5):
        engine._reconcile_idle_demand()
    assert len(events) == 4
    assert all(event.payload["status"] == ("failed" if error_code else "ready")
               for event in events[2:])
    if error_code:
        assert all(event.payload["message"] == "Idle media unavailable; showing year text"
                   for event in events[2:])


def test_owner_transport_error_reads_only_the_current_producer_without_owner_lock():
    from solin.core.scenes.libobs_idle_source import LibobsIdleSource

    class ForbiddenLock:
        def __enter__(self):
            pytest.fail("Render health must not acquire the idle owner's lock")

        def __exit__(self, *_args):
            return False

    owner = LibobsIdleSource(SimpleNamespace())
    owner._lock = ForbiddenLock()
    retired = SimpleNamespace(transport=SimpleNamespace(error_code="idle_media_unavailable"))
    owner._producer = retired
    assert owner.transport_error_code == "idle_media_unavailable"
    owner._producer = SimpleNamespace(transport=SimpleNamespace(error_code=""))
    assert owner.transport_error_code == ""
    retired.transport.error_code = "idle_source_unavailable"
    assert owner.transport_error_code == ""
    owner._producer = SimpleNamespace(transport=None)
    assert owner.transport_error_code == ""
    owner._producer = None
    assert owner.transport_error_code == ""


def test_transport_notification_releases_the_graph_guard_before_publishing():
    engine, idle, events = _health_engine()
    guard = threading.Lock()
    engine._window_output.hydrate_lock = guard

    def receive(event):
        assert guard.acquire(blocking=False)
        guard.release()
        events.append(event)

    engine.set_event_sink(receive)
    idle.transport_error_code = "idle_media_unavailable"
    engine._reconcile_idle_demand()
    assert len(events) == 2


def test_superseded_producer_error_is_not_returned_from_the_lock_free_snapshot():
    from solin.core.scenes.libobs_idle_source import LibobsIdleSource

    owner = LibobsIdleSource(SimpleNamespace())

    class SupersededTransport:
        @property
        def error_code(self):
            owner._producer = SimpleNamespace(transport=None)
            return "idle_media_unavailable"

    owner._producer = SimpleNamespace(transport=SupersededTransport())
    assert owner.transport_error_code == ""


@pytest.mark.parametrize("scene_id", ["idle", "nested-idle"])
def test_thumbnails_alone_keep_a_shared_loop_running(scene_id):
    engine, idle, pointer, trees = _engine()
    engine._thumbnail_egress = SimpleNamespace(scene_ids=("other", scene_id))
    engine._reconcile_idle_demand()
    assert idle.demand and idle.restarts == 1
    # First live entry restarts even though thumbnails were already running.
    trees[0].pointers = (pointer,)
    engine._virtual_camera.active = True
    engine._thumbnail_egress.scene_ids = ()
    engine._reconcile_idle_demand()
    assert idle.restarts == 2 and idle.pauses == 0
    engine._virtual_camera.active = False
    engine._reconcile_idle_demand()
    assert idle.pauses == 1


@pytest.mark.parametrize("route,index,scene_id", [
    ("program", 0, ""), ("projection", 1, ""), ("scene", 2, "editor"),
])
def test_live_displays_count_without_readback_or_output_enablement(route, index, scene_id):
    engine, idle, pointer, trees = _engine()
    trees[index].pointers = (pointer,)
    engine._window_output.render_targets = ((route, scene_id),)
    engine._reconcile_idle_demand()
    assert idle.demand
    engine._window_output.render_targets = ((route, scene_id),) * 3
    engine._reconcile_idle_demand()
    assert idle.restarts == 1
    engine._window_output.render_targets = ()
    engine._reconcile_idle_demand()
    assert idle.pauses == 1


def test_source_isolation_counts_the_actual_preview_instead_of_authored_layer_visibility():
    engine, idle, pointer, trees = _engine()
    trees[2].pointers = (pointer,)
    engine._preview_egress.active = True
    # A hidden idle layer can still be displayed in source isolation while framing.
    engine._scene_graph.scene_uses_idle_source = lambda scene_id: False
    engine._reconcile_idle_demand()
    assert idle.demand


def test_rebuild_and_native_borrower_guards_preserve_demand_without_blocking():
    engine, idle, pointer, trees = _engine()
    trees[0].pointers = (pointer,)
    engine._virtual_camera.active = True
    engine._reconcile_idle_demand()
    engine._idle_suspended = True
    trees[0].pointers = ()
    engine._reconcile_idle_demand()
    assert idle.demand
    engine._idle_suspended = False
    guard = threading.Lock()
    engine._window_output.hydrate_lock = guard
    with guard:
        engine._reconcile_idle_demand()
        assert idle.demand
    trees[0].pointers = (pointer,)
    engine._reconcile_idle_demand()
    assert idle.restarts == 1 and idle.pauses == 0


def test_removing_all_idle_definitions_and_consumers_pauses_the_retained_owner():
    engine, idle, _, _ = _engine()
    engine._thumbnail_egress = SimpleNamespace(scene_ids=("idle",))
    engine._reconcile_idle_demand()
    engine._idle_definitions = ()
    engine._thumbnail_egress.scene_ids = ()
    engine._reconcile_idle_demand()
    assert not idle.demand and idle.pauses == 1


def test_empty_owner_does_not_create_a_source_from_a_render_callback():
    engine, idle, _, _ = _engine()
    engine._idle_scene_source = None
    engine._reconcile_idle_demand()
    assert not idle.demand


def test_aggregate_render_flags_are_not_consumers_of_the_idle_source():
    engine, idle, pointer, trees = _engine()
    trees[0].pointers = trees[1].pointers = (pointer,)
    # Projection can require Program compositing while showing another scene.
    # That coarse scheduling flag is not evidence that anyone sees Program idle.
    engine._render_demand = {"virtual_camera": True, "media_windows": True, "editor": True}
    engine._reconcile_idle_demand()
    assert not idle.demand


def test_first_live_entry_restarts_preview_loop_but_additional_live_consumers_do_not():
    engine, idle, pointer, trees = _engine()
    trees[0].pointers = trees[1].pointers = (pointer,)
    engine._thumbnail_egress = SimpleNamespace(scene_ids=("idle",))
    engine._reconcile_idle_demand()
    assert idle.restarts == 1 and not idle.live
    engine._window_output.render_targets = (("projection", ""),)
    engine._reconcile_idle_demand()
    assert idle.restarts == 2 and idle.live
    engine._window_output.render_targets *= 3
    engine._virtual_camera.active = True
    engine._reconcile_idle_demand()
    assert idle.restarts == 2
    engine._window_output.render_targets = ()
    engine._reconcile_idle_demand()
    assert idle.live and idle.restarts == 2
    engine._virtual_camera.active = False
    engine._reconcile_idle_demand()
    assert idle.demand and not idle.live and idle.pauses == 0
    engine._window_output.render_targets = (("projection", ""),)
    engine._reconcile_idle_demand()
    assert idle.restarts == 3
    engine._window_output.render_targets = ()
    engine._thumbnail_egress.scene_ids = ()
    engine._reconcile_idle_demand()
    assert not idle.demand and idle.pauses == 1
