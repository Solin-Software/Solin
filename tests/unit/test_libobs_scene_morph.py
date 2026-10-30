"""The merge transition: pairing layers, interpolating them and running the morph."""

from __future__ import annotations

import types

import pytest

from solin.core.scenes.libobs_projection_route import LibobsProjectionRoute
from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph
from solin.core.scenes.libobs_scene_morph import (
    MorphRole,
    interpolate_layer,
    plan_scene_morph,
)
from tests.unit.test_libobs_scene_engine import _CompositingRuntime


def _record(source_id, *, native=None, is_content=False, reference="", content_source=None):
    return {
        "layer": {"id": f"layer-{source_id}", "source_id": source_id},
        "item": types.SimpleNamespace(source=native or f"native-{id(object())}"),
        "is_content": is_content,
        "referenced_scene_id": reference,
        "content_source": content_source,
    }


def _roles(plan):
    return [(entry.role, entry.record["layer"]["source_id"]) for entry in plan]


def test_plan_moves_shared_sources_and_fades_the_rest():
    camera_a, camera_b = _record("camera"), _record("camera")
    plan = plan_scene_morph([_record("old"), camera_a], [camera_b, _record("new")])

    assert _roles(plan) == [
        (MorphRole.FADE_OUT, "old"),
        (MorphRole.MOVE, "camera"),
        (MorphRole.FADE_IN, "new"),
    ]
    assert plan[1].record is camera_a and plan[1].destination is camera_b


def test_plan_keeps_an_origin_only_layer_above_the_layer_it_covered():
    plan = plan_scene_morph(
        [_record("camera"), _record("lower-third"), _record("logo")],
        [_record("logo"), _record("camera")],
    )

    assert _roles(plan) == [
        (MorphRole.MOVE, "logo"),
        (MorphRole.MOVE, "camera"),
        (MorphRole.FADE_OUT, "lower-third"),
    ]


def test_plan_pairs_a_repeated_source_once_per_layer():
    plan = plan_scene_morph([_record("camera")], [_record("camera"), _record("camera")])

    assert _roles(plan) == [(MorphRole.MOVE, "camera"), (MorphRole.FADE_IN, "camera")]


def test_plan_fades_content_that_changed_generation():
    plan = plan_scene_morph(
        [_record("content", native="old-media", is_content=True)],
        [_record("content", native="new-media", is_content=True)],
    )

    assert {role for role, _source in _roles(plan)} == {MorphRole.FADE_IN, MorphRole.FADE_OUT}


def test_plan_moves_content_that_stays_the_same():
    plan = plan_scene_morph(
        [_record("content", native="media", is_content=True)],
        [_record("content", native="media", is_content=True)],
    )

    assert _roles(plan) == [(MorphRole.MOVE, "content")]


def test_interpolation_reaches_both_ends_and_turns_the_short_way():
    start = {
        "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        "rotation_degrees": 350.0,
        "fit_mode": "contain",
        "mirror_x": False,
    }
    end = {
        "rect": {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
        "crop": {"left": 0.2, "top": 0.0, "right": 0.0, "bottom": 0.0},
        "rotation_degrees": 10.0,
        "fit_mode": "cover",
        "mirror_x": True,
    }

    middle = interpolate_layer(start, end, 0.5)

    assert middle["rect"] == {"x": 0.25, "y": 0.25, "width": 0.75, "height": 0.75}
    assert middle["crop"]["left"] == pytest.approx(0.1)
    assert middle["rotation_degrees"] % 360 == pytest.approx(0.0)
    assert (middle["fit_mode"], middle["mirror_x"]) == ("cover", True)
    assert interpolate_layer(start, end, 0.0)["fit_mode"] == "contain"
    assert interpolate_layer(start, end, 1.0)["rect"] == end["rect"]


# ── the graph ────────────────────────────────────────────────────────────────


def _layer(layer_id, source_id, x=0.0):
    return {
        "id": layer_id,
        "source_id": source_id,
        "visible": True,
        "rect": {"x": x, "y": 0.0, "width": 0.5, "height": 0.5},
    }


_DOCUMENT = {
    "sources": [
        {"id": "shared", "type": "color", "configuration": {"color": "#00ff00"}},
        {"id": "only-a", "type": "color", "configuration": {"color": "#ff0000"}},
        {"id": "only-b", "type": "color", "configuration": {"color": "#0000ff"}},
    ],
    "scenes": [
        {"id": "a", "layers": [_layer("a1", "only-a"), _layer("a2", "shared")]},
        {"id": "b", "layers": [_layer("b1", "shared", x=0.5), _layer("b2", "only-b")]},
        {"id": "c", "layers": [_layer("c1", "only-b")]},
    ],
}


@pytest.fixture
def fades(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "solin.core.scenes.libobs_scene_morph._start_item_fade",
        lambda _item, _fade, *, show, duration_ms: calls.append(show),
    )
    lib = types.SimpleNamespace(
        obs_source_inc_showing=lambda _pointer: None,
        obs_source_dec_showing=lambda _pointer: None,
    )
    monkeypatch.setattr("pylibobs._ffi.get_lib", lambda: lib, raising=False)
    return calls


@pytest.fixture
def graph(fades, monkeypatch):
    # Tests step the morph themselves, so the ticker never races an assertion.
    monkeypatch.setattr(LibobsSceneGraph, "_run_morphs", lambda _self: None)
    graph = LibobsSceneGraph(_CompositingRuntime())
    graph.hydrate(_DOCUMENT, {"virtual_camera": "a"})
    yield graph
    graph._morph_thread = None
    graph.clear()


def _take(graph, scene_id, kind="merge"):
    prepared = graph.prepare(scene_id, kind, 400)
    assert prepared is not None and prepared["fallback_applied"] is False
    return graph.take(prepared["token"])


def _program(graph):
    return graph.program_source


def test_merge_routes_the_morph_scene_and_ends_on_the_destination(graph, fades):
    assert _take(graph, "b") is True

    assert _program(graph).kind == "cut_transition"
    assert _program(graph).current_source.startswith("scene-source:solin-morph-")
    assert fades == [False, True]  # only-a fades out, only-b fades in
    assert graph.active_scene_source == graph.scene_source("b")

    graph._tick_morphs(float("inf"))

    assert _program(graph).current_source == graph.scene_source("b")
    assert graph._morphs == {}


def test_merge_moves_the_shared_layer_towards_its_destination(graph):
    _take(graph, "b")
    morph = graph._morphs["virtual_camera"]
    item, _start, _end = morph._moving[0]
    positions = []
    for elapsed in (0.0, 0.2, 0.39):
        morph.tick(morph._started_at + elapsed)
        positions.append(item.pos[0])

    assert positions[0] < positions[1] < positions[2] <= 1440.0


def test_merge_without_shared_sources_dissolves(graph):
    assert _take(graph, "c") is True

    assert graph._morphs == {}
    assert _program(graph).kind == "fade_transition"
    assert _program(graph).starts[-1][0] == graph.scene_source("c")


def test_repeated_merge_take_does_not_restart_the_morph(graph):
    _take(graph, "b")
    morph = graph._morphs["virtual_camera"]

    assert _take(graph, "b") is True

    assert graph._morphs["virtual_camera"] is morph


def test_new_take_during_a_merge_starts_from_its_destination(graph):
    _take(graph, "b")
    first = graph._morphs["virtual_camera"]

    assert _take(graph, "a") is True

    second = graph._morphs["virtual_camera"]
    assert second is not first
    assert first._scene is None  # released
    assert second.destination_source == graph.scene_source("a")


def test_other_kind_during_a_merge_takes_over(graph):
    _take(graph, "b")

    assert _take(graph, "a", "dissolve") is True

    assert graph._morphs == {}
    assert _program(graph).kind == "fade_transition"
    assert _program(graph).starts[-1][0] == graph.scene_source("a")


def test_rebuild_ends_a_merge_before_releasing_the_graph(graph):
    _take(graph, "b")
    morph = graph._morphs["virtual_camera"]

    graph.hydrate(_DOCUMENT, {"virtual_camera": "a"})

    assert graph._morphs == {}
    assert morph._scene is None
    assert _program(graph).current_source == graph.scene_source("a")


def test_failed_build_falls_back_to_a_dissolve(graph, monkeypatch):
    def refuse(_name):
        raise RuntimeError("no scene")

    monkeypatch.setattr(graph._runtime.ob.Scene, "create_private", refuse)

    assert _take(graph, "b") is True

    assert _program(graph).kind == "fade_transition"
    assert graph.active_scene_source == graph.scene_source("b")


def test_morph_keeps_its_item_fades_and_crops_out_of_the_graph_after_release(graph):
    _take(graph, "b")
    fades_owned = [fade for _item, fade, _show in graph._morphs["virtual_camera"]._fading]

    graph._tick_morphs(float("inf"))

    assert all(fade.released == 1 for fade in fades_owned)
    assert graph._cropped_items == {}


def test_failed_completion_keeps_the_morph_scene_until_the_rebuild(graph):
    _take(graph, "b")
    morph = graph._morphs["virtual_camera"]
    program = _program(graph)
    original = program.set_source

    def refuse(_source):
        raise RuntimeError("no route")

    program.set_source = refuse
    graph._tick_morphs(float("inf"))

    assert graph._morphs == {}
    assert morph._scene is not None

    program.set_source = original
    graph.hydrate(_DOCUMENT, {"virtual_camera": "a"})

    assert morph._scene is None


def test_content_take_fades_the_content_it_replaces(fades, monkeypatch):
    monkeypatch.setattr(LibobsSceneGraph, "_run_morphs", lambda _self: None)
    document = {
        "sources": [
            {"id": "content", "type": "solin_content", "configuration": {}},
            {"id": "logo", "type": "color", "configuration": {"color": "#00ff00"}},
        ],
        "scenes": [
            {"id": "a", "layers": [_layer("a1", "content"), _layer("a2", "logo")]},
            {"id": "b", "layers": [_layer("b1", "content", x=0.5), _layer("b2", "logo", x=0.5)]},
        ],
    }
    runtime = _CompositingRuntime()
    old = runtime.ob.Source.create("color_source_v3", "old", {})
    new = runtime.ob.Source.create("color_source_v3", "new", {})
    graph = LibobsSceneGraph(runtime)
    graph.hydrate(document, {"virtual_camera": "a"}, old)

    with graph.content_presentation(new) as presentation:
        assert graph._begin_morph(
            "virtual_camera", "b", "a", graph.scene_source("a"),
            presentation.scene_source("b"), 400,
            show=lambda _source: None, complete=lambda: None,
        ) is True
        morph = graph._morphs["virtual_camera"]
        faded = {
            entry.role: entry.record["item"].source
            for entry in morph._plan
            if entry.role is not MorphRole.MOVE
        }
        assert faded == {MorphRole.FADE_OUT: old, MorphRole.FADE_IN: new}
        graph._morph_thread = None
        graph._morphs.clear()
        morph.release()
    graph.clear()


def test_content_shown_by_a_morph_stays_borrowed():
    morph_content = object()
    graph = LibobsSceneGraph(_CompositingRuntime())
    graph._morphs["virtual_camera"] = types.SimpleNamespace(content_sources=(morph_content,))

    assert morph_content in graph.content_sources


# ── the projection ───────────────────────────────────────────────────────────


@pytest.fixture
def route(graph):
    route = LibobsProjectionRoute(graph._runtime)
    route.prepare_all()
    route.set_scene("a", graph.scene_source("a"))
    yield route
    route.shutdown()


def _take_projection(graph, route, scene_id):
    prepared = graph.prepare(scene_id, "merge", 400, "media_windows")
    return graph.take_projection(prepared["token"], route)


def test_projection_merges_on_its_own_transition(graph, route):
    assert _take_projection(graph, route, "b") is True

    assert route.transition_source.current_source.startswith("scene-source:solin-morph-")
    assert route.scene_id == "b"
    assert route.scene_source == graph.scene_source("b")
    assert graph.active_scene_source == graph.scene_source("a")  # Program did not move

    graph._tick_morphs(float("inf"))

    assert route.transition_source.current_source == graph.scene_source("b")


def test_finishing_merge_does_not_undo_a_later_projection_cut(graph, route):
    _take_projection(graph, route, "b")

    route.set_scene("c", graph.scene_source("c"))
    graph._tick_morphs(float("inf"))

    assert route.transition_source.current_source == graph.scene_source("c")


def test_failed_cut_during_a_merge_still_ends_on_its_destination(graph, route):
    _take_projection(graph, route, "b")
    transition = route.transition_source
    original = transition.set_source

    def refuse(_source):
        raise RuntimeError("no route")

    transition.set_source = refuse
    assert route.set_scene("c", graph.scene_source("c")) is False
    transition.set_source = original

    graph._tick_morphs(float("inf"))

    assert transition.current_source == graph.scene_source("b")
    assert route.scene_id == "b"


def test_projection_merge_never_flashes_the_destination(graph, route):
    seen = []
    route.start("a", graph.scene_source("a"), "dissolve", 100)  # not on the cut transition
    cut = route._transitions.prepared("cut")
    original = cut.set_source
    cut.set_source = lambda source: (seen.append(source), original(source))

    _take_projection(graph, route, "b")

    assert graph.scene_source("b") not in seen
