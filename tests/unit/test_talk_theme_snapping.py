from __future__ import annotations

from solin.core.talk_theme.snapping import LayerGeometry, benchmark_snap, snap_layer


def test_snap_layer_aligns_center_with_hysteresis_metadata() -> None:
    result = snap_layer(
        LayerGeometry("title", 0.295, 0.4, 0.4, 0.1),
        viewport_width=1000,
        viewport_height=600,
    )

    assert result.x == 0.3
    assert result.snap_x == "center"
    assert result.guides[0].id == "center"


def test_middle_guide_aligns_the_box_center_instead_of_its_top_edge() -> None:
    centered = snap_layer(
        LayerGeometry("title", 0.2, 0.444, 0.4, 0.1),
        viewport_width=1000,
        viewport_height=600,
    )
    top_near_middle = snap_layer(
        LayerGeometry("title", 0.2, 0.494, 0.4, 0.1),
        viewport_width=1000,
        viewport_height=600,
    )

    assert centered.y == 0.45
    assert centered.snap_y == "middle"
    assert top_near_middle.snap_y == ""


def test_snap_can_be_disabled_and_axis_locked() -> None:
    moving = LayerGeometry("title", 0.295, 0.4, 0.4, 0.1)

    disabled = snap_layer(
        moving,
        viewport_width=1000,
        viewport_height=600,
        disabled=True,
    )
    locked = snap_layer(
        moving,
        viewport_width=1000,
        viewport_height=600,
        lock_axis="x",
        origin_y=0.25,
    )

    assert disabled.x == moving.x
    assert disabled.snap_x == ""
    assert locked.y == 0.25


def test_active_snap_uses_a_wider_release_threshold() -> None:
    moving = LayerGeometry("title", 0.309, 0.4, 0.4, 0.1)

    inactive = snap_layer(
        moving,
        viewport_width=1000,
        viewport_height=600,
    )
    active = snap_layer(
        moving,
        viewport_width=1000,
        viewport_height=600,
        active_x="center",
    )

    assert inactive.snap_x == ""
    assert active.snap_x == "center"
    assert active.x == 0.3


def test_peer_alignment_guides_do_not_expose_a_generic_badge() -> None:
    result = snap_layer(
        LayerGeometry("moving", 0.215, 0.4, 0.1, 0.1),
        viewport_width=1000,
        viewport_height=600,
        others=(LayerGeometry("peer", 0.22, 0.7, 0.2, 0.1),),
    )

    assert result.snap_x == "peer:left"
    assert result.guides[0].id == "peer:left"


def test_snap_engine_p95_stays_below_one_millisecond() -> None:
    result = benchmark_snap(2_000)

    assert result["p95_ms"] < 1.0
