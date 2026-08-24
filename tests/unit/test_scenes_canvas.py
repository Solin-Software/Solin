from __future__ import annotations

import pytest

from solin.core.scenes.model import Crop, NormalizedRect
from solin.core.scenes.editor_geometry import crop_geometry, resize_rect, snap_move_rect


def test_resize_preserves_aspect_by_default() -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=0.4, height=0.3)

    resized = resize_rect(
        original,
        "bottom_right",
        0.2,
        0.04,
        preserve_aspect=True,
    )

    assert resized.width / resized.height == pytest.approx(original.width / original.height)
    assert resized.x == original.x
    assert resized.y == original.y


def test_shift_resize_can_change_aspect() -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=0.4, height=0.3)

    resized = resize_rect(
        original,
        "bottom_right",
        0.2,
        0.04,
        preserve_aspect=False,
    )

    assert resized.width == pytest.approx(0.6)
    assert resized.height == pytest.approx(0.34)


def test_free_resize_clamps_a_drag_past_the_opposite_edge() -> None:
    original = NormalizedRect(x=0.35, y=0.45, width=0.08, height=0.45)

    resized = resize_rect(
        original,
        "bottom_right",
        -1.0,
        -1.0,
        preserve_aspect=False,
    )

    assert resized.x == original.x
    assert resized.y == original.y
    assert resized.width == pytest.approx(0.02)
    assert resized.height == pytest.approx(0.02)


def test_alt_crop_moves_the_visible_edge_without_rescaling_content() -> None:
    original = NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8)

    rect, crop = crop_geometry(original, Crop(), "left", 0.2, 0.0)

    assert rect.x == pytest.approx(0.3)
    assert rect.width == pytest.approx(0.6)
    assert crop.left == pytest.approx(0.25)


def test_crop_can_be_expanded_only_to_the_original_source_boundary() -> None:
    original = NormalizedRect(x=0.3, y=0.1, width=0.6, height=0.8)
    original_crop = Crop(left=0.25)

    rect, crop = crop_geometry(original, original_crop, "left", -0.5, 0.0)

    assert crop.left == 0.0
    assert rect.x == pytest.approx(0.1)
    assert rect.width == pytest.approx(0.8)


def test_move_snaps_to_canvas_center_and_safe_area_edges() -> None:
    centered, center_guides = snap_move_rect(
        NormalizedRect(x=0.297, y=0.2, width=0.4, height=0.3),
        viewport_width=1000,
        viewport_height=600,
    )
    edged, edge_guides = snap_move_rect(
        NormalizedRect(x=0.034, y=0.655, width=0.4, height=0.3),
        viewport_width=1000,
        viewport_height=600,
    )

    assert centered.x == pytest.approx(0.3)
    assert ("x", 0.5) in center_guides
    assert edged.x == pytest.approx(0.03)
    assert edged.y == pytest.approx(0.65)
    assert {guide for guide in edge_guides} == {("x", 0.03), ("y", 0.95)}


def test_move_snap_keeps_a_visible_margin_at_every_corner() -> None:
    expected = (
        (0.03, 0.05),
        (0.67, 0.05),
        (0.03, 0.65),
        (0.67, 0.65),
    )

    for expected_x, expected_y in expected:
        candidate = NormalizedRect(
            x=expected_x + (0.003 if expected_x < 0.5 else -0.003),
            y=expected_y + (0.003 if expected_y < 0.5 else -0.003),
            width=0.3,
            height=0.3,
        )
        snapped, guides = snap_move_rect(
            candidate,
            viewport_width=1000,
            viewport_height=600,
        )

        assert snapped.x == pytest.approx(expected_x)
        assert snapped.y == pytest.approx(expected_y)
        assert len(guides) == 2


def test_safe_area_uses_the_same_pixel_margin_on_both_axes() -> None:
    snapped, guides = snap_move_rect(
        NormalizedRect(x=0.031, y=0.052, width=0.25, height=0.25),
        viewport_width=1600,
        viewport_height=900,
    )

    horizontal_margin = snapped.x * 1600
    vertical_margin = snapped.y * 900
    assert horizontal_margin == pytest.approx(45.0)
    assert vertical_margin == pytest.approx(45.0)
    assert dict(guides)["x"] == pytest.approx(45 / 1600)
    assert dict(guides)["y"] == pytest.approx(45 / 900)


def test_control_disables_canvas_snapping() -> None:
    original = NormalizedRect(x=0.297, y=0.004, width=0.4, height=0.3)

    moved, guides = snap_move_rect(
        original,
        viewport_width=1000,
        viewport_height=600,
        disabled=True,
    )

    assert moved == original
    assert guides == ()


def test_canvas_snap_hysteresis_avoids_flicker_near_release_threshold() -> None:
    almost_released = NormalizedRect(x=0.309, y=0.17, width=0.4, height=0.3)

    unsnapped, no_guides = snap_move_rect(
        almost_released,
        viewport_width=1000,
        viewport_height=600,
    )
    held, guides = snap_move_rect(
        almost_released,
        viewport_width=1000,
        viewport_height=600,
        active_guides=(("x", 0.5),),
    )

    assert unsnapped == almost_released
    assert no_guides == ()
    assert held.x == pytest.approx(0.3)
    assert guides == (("x", 0.5),)
