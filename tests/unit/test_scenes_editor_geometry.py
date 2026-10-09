from __future__ import annotations

import pytest

from solin.core.scenes.editor_geometry import (
    crop_for_framing_rect,
    crop_geometry,
    expand_focus_rect_to_aspect,
    fill_crop_to_canvas,
    move_framing_rect,
    recover_uncropped_rect,
    resize_framing_rect,
    resize_rect,
    scale_framing_rect,
    snap_move_rect,
    source_framing_geometry,
)
from solin.core.scenes.model import Crop, FitMode, NormalizedRect, SceneLayer


def test_editor_resize_preserves_aspect_unless_free_resize_is_requested() -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=0.4, height=0.3)

    preserved = resize_rect(original, "bottom_right", 0.2, 0.04, preserve_aspect=True)
    free = resize_rect(original, "bottom_right", 0.2, 0.04, preserve_aspect=False)

    assert preserved.width / preserved.height == pytest.approx(
        original.width / original.height
    )
    assert free.width == pytest.approx(0.6)
    assert free.height == pytest.approx(0.34)


def test_editor_crop_changes_visible_bounds_without_rescaling_the_source() -> None:
    rect, crop = crop_geometry(
        NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8),
        Crop(),
        "left",
        0.2,
        0.0,
    )

    assert rect.x == pytest.approx(0.3)
    assert rect.width == pytest.approx(0.6)
    assert crop.left == pytest.approx(0.25)


@pytest.mark.parametrize("source_size", [(1920, 1080), (1440, 1080), (1080, 1920), (3840, 1080)])
@pytest.mark.parametrize("canvas_size", [(1920, 1080), (1080, 1920), (1440, 1080)])
@pytest.mark.parametrize("fit_mode", [FitMode.CONTAIN, FitMode.COVER, FitMode.STRETCH])
def test_full_source_framing_fits_physical_aspect_and_proposes_a_canvas_aspect_focus(
    source_size: tuple[int, int], canvas_size: tuple[int, int], fit_mode: FitMode,
) -> None:
    layer = SceneLayer(id="layer", source_id="source", name="Source", fit_mode=fit_mode,
                       crop=Crop(left=0.1, top=0.2, right=0.3, bottom=0.1))
    source_width, source_height = source_size
    canvas_width, canvas_height = canvas_size
    bounds, frame = source_framing_geometry(
        layer, source_width=source_width, source_height=source_height,
        canvas_width=canvas_width, canvas_height=canvas_height,
    )
    assert bounds.width * canvas_width / (bounds.height * canvas_height) == pytest.approx(
        source_width / source_height,
    )
    assert 0 <= bounds.x < bounds.x + bounds.width <= 1
    assert 0 <= bounds.y < bounds.y + bounds.height <= 1
    assert frame.width == pytest.approx(frame.height)
    crop = crop_for_framing_rect(bounds, Crop(), frame)
    # The proposed frame lies in the current focus, without mutating its crop.
    assert crop.left >= layer.crop.left - 1e-9
    assert crop.top >= layer.crop.top - 1e-9
    assert crop.right >= layer.crop.right - 1e-9
    assert crop.bottom >= layer.crop.bottom - 1e-9


def test_full_source_framing_includes_implicit_cover_crop_and_mirrored_focus() -> None:
    layer = SceneLayer(id="layer", source_id="source", name="Portrait", fit_mode=FitMode.COVER)
    bounds, frame = source_framing_geometry(
        layer, source_width=1080, source_height=1920, canvas_width=1920, canvas_height=1080,
    )
    assert bounds.height == 1.0
    assert bounds.width == pytest.approx((1080 / 1920) / (1920 / 1080))
    crop = crop_for_framing_rect(bounds, Crop(), frame)
    assert crop.left == crop.right == 0
    assert crop.top == pytest.approx((1 - bounds.width) / 2)
    assert crop.bottom == pytest.approx(crop.top)


@pytest.mark.parametrize("dimensions", [(0, 1080, 1920, 1080), (1920, -1, 1920, 1080),
                                         (1920, 1080, 0, 1080), (True, 1080, 1920, 1080)])
def test_full_source_framing_rejects_unknown_or_invalid_dimensions(dimensions) -> None:
    with pytest.raises(ValueError):
        source_framing_geometry(
            SceneLayer(id="layer", source_id="source", name="Source"),
            source_width=dimensions[0], source_height=dimensions[1],
            canvas_width=dimensions[2], canvas_height=dimensions[3],
        )


@pytest.mark.parametrize("delta", [-1.0, 1.0])
def test_moving_a_tiny_source_focus_stays_within_valid_crop_edges(delta: float) -> None:
    layer = SceneLayer(id="layer", source_id="source", name="Source",
                       crop=Crop(left=0.99, top=0.99, right=0.0095, bottom=0.0095))
    bounds, frame = source_framing_geometry(
        layer, source_width=1920, source_height=1080, canvas_width=1920, canvas_height=1080,
    )
    moved = move_framing_rect(frame, bounds, delta, delta)
    crop = crop_for_framing_rect(bounds, Crop(), moved)
    assert max(crop.left, crop.top, crop.right, crop.bottom) <= 0.99


@pytest.mark.parametrize("source_size", [(4096, 16), (16, 4096)])
def test_scaling_extreme_source_aspects_keeps_crop_edges_representable(source_size) -> None:
    bounds, frame = source_framing_geometry(
        SceneLayer(id="layer", source_id="source", name="Source"),
        source_width=source_size[0], source_height=source_size[1],
        canvas_width=1920, canvas_height=1080,
    )
    scaled = scale_framing_rect(frame, bounds, 0.5, bounds.x, bounds.y)
    crop = crop_for_framing_rect(bounds, Crop(), scaled)
    assert max(crop.left, crop.top, crop.right, crop.bottom) <= 0.99


def test_editor_safe_margin_is_five_percent_of_the_physical_short_edge() -> None:
    snapped, guides = snap_move_rect(
        NormalizedRect(x=0.034, y=0.655, width=0.4, height=0.3),
        viewport_width=1000,
        viewport_height=600,
    )

    assert snapped.x == pytest.approx(0.03)
    assert snapped.y == pytest.approx(0.65)
    assert set(guides) == {("x", 0.03), ("y", 0.95)}


def test_fill_crop_keeps_an_existing_canvas_aspect_focus() -> None:
    crop = Crop(left=0.2, top=0.1, right=0.4, bottom=0.5)
    rect = NormalizedRect(x=0.2, y=0.1, width=0.4, height=0.4)

    assert fill_crop_to_canvas(rect, crop) == crop


def test_fill_crop_expands_the_deficient_axis_before_tightening() -> None:
    crop = Crop(left=0.3, top=0.1, right=0.4, bottom=0.1)
    rect = NormalizedRect(x=0.3, y=0.1, width=0.3, height=0.8)

    framed = fill_crop_to_canvas(rect, crop)

    assert framed.left == pytest.approx(0.05)
    assert framed.right == pytest.approx(0.15)
    assert framed.top == pytest.approx(0.1)
    assert framed.bottom == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("crop", "rect", "expected"),
    [
        (
            Crop(left=0.0, top=0.1, right=0.7, bottom=0.1),
            NormalizedRect(x=0.0, y=0.1, width=0.3, height=0.8),
            Crop(left=0.0, top=0.35, right=0.7, bottom=0.35),
        ),
        (
            Crop(left=0.7, top=0.1, right=0.0, bottom=0.1),
            NormalizedRect(x=0.7, y=0.1, width=0.3, height=0.8),
            Crop(left=0.7, top=0.35, right=0.0, bottom=0.35),
        ),
        (
            Crop(left=0.1, top=0.0, right=0.1, bottom=0.7),
            NormalizedRect(x=0.1, y=0.0, width=0.8, height=0.3),
            Crop(left=0.35, top=0.0, right=0.35, bottom=0.7),
        ),
        (
            Crop(left=0.1, top=0.7, right=0.1, bottom=0.0),
            NormalizedRect(x=0.1, y=0.7, width=0.8, height=0.3),
            Crop(left=0.35, top=0.7, right=0.35, bottom=0.0),
        ),
    ],
    ids=("left", "right", "top", "bottom"),
)
def test_fill_crop_tightens_the_other_axis_when_centered_expansion_hits_an_edge(
    crop: Crop,
    rect: NormalizedRect,
    expected: Crop,
) -> None:

    framed = fill_crop_to_canvas(rect, crop)

    assert framed.left == pytest.approx(expected.left)
    assert framed.right == pytest.approx(expected.right)
    assert framed.top == pytest.approx(expected.top)
    assert framed.bottom == pytest.approx(expected.bottom)


def test_fill_crop_expands_vertically_around_the_same_focus_center() -> None:
    crop = Crop(left=0.1, top=0.3, right=0.1, bottom=0.4)
    rect = NormalizedRect(x=0.1, y=0.3, width=0.8, height=0.3)

    framed = fill_crop_to_canvas(rect, crop)

    assert framed.left == pytest.approx(0.1)
    assert framed.right == pytest.approx(0.1)
    assert framed.top == pytest.approx(0.05)
    assert framed.bottom == pytest.approx(0.15)


def test_framing_geometry_maps_mirrored_visual_edges_back_to_source_edges() -> None:
    crop = Crop(left=0.2, top=0.3, right=0.4, bottom=0.3)
    visible = NormalizedRect(x=0.4, y=0.3, width=0.4, height=0.4)

    bounds = recover_uncropped_rect(visible, crop, mirror_x=True)
    framed = fill_crop_to_canvas(visible, crop, mirror_x=True)
    left_visual_half = crop_for_framing_rect(
        NormalizedRect(),
        Crop(),
        NormalizedRect(x=0.0, y=0.0, width=0.5, height=1.0),
        mirror_x=True,
    )
    top_visual_half = crop_for_framing_rect(
        NormalizedRect(),
        Crop(),
        NormalizedRect(x=0.0, y=0.0, width=1.0, height=0.5),
        mirror_y=True,
    )

    assert bounds == NormalizedRect()
    assert framed == crop
    assert left_visual_half == Crop(left=0.5)
    assert top_visual_half == Crop(top=0.5)


def test_framing_preserves_an_asymmetric_focus_center_at_minimum_size() -> None:
    focus = NormalizedRect(x=0.17, y=0.31, width=0.01, height=0.04)
    frame = expand_focus_rect_to_aspect(focus, NormalizedRect())
    scaled = scale_framing_rect(
        frame,
        NormalizedRect(),
        0.001,
        frame.x + frame.width / 2.0,
        frame.y + frame.height / 2.0,
        minimum_width=0.01,
    )
    crop = crop_for_framing_rect(NormalizedRect(), Crop(), scaled)

    assert scaled.width == pytest.approx(0.02)
    assert scaled.height == pytest.approx(0.02)
    assert scaled.x + scaled.width / 2.0 == pytest.approx(
        focus.x + focus.width / 2.0
    )
    assert scaled.y + scaled.height / 2.0 == pytest.approx(
        focus.y + focus.height / 2.0
    )
    assert 1.0 - crop.left - crop.right == pytest.approx(0.02)
    assert 1.0 - crop.top - crop.bottom == pytest.approx(0.02)


@pytest.mark.parametrize("output_size", [(1440, 1080), (1080, 1920)])
def test_normalized_canvas_aspect_matches_any_physical_output(
    output_size: tuple[int, int],
) -> None:
    crop = Crop(left=0.25, top=0.1, right=0.45, bottom=0.2)
    visible = NormalizedRect(x=0.25, y=0.1, width=0.3, height=0.7)
    framed = fill_crop_to_canvas(visible, crop)
    output_width, output_height = output_size
    framed_width = 1.0 - framed.left - framed.right
    framed_height = 1.0 - framed.top - framed.bottom

    physical_aspect = framed_width * output_width / (framed_height * output_height)

    assert physical_aspect == pytest.approx(output_width / output_height)
    assert framed.left + framed_width / 2.0 == pytest.approx(
        crop.left + visible.width / 2.0
    )
    assert framed.top + framed_height / 2.0 == pytest.approx(
        crop.top + visible.height / 2.0
    )


def test_framing_move_resize_and_wheel_scale_stay_inside_available_bounds() -> None:
    bounds = NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.8)
    original = NormalizedRect(x=0.3, y=0.3, width=0.4, height=0.4)

    moved = move_framing_rect(original, bounds, 1.0, -1.0)
    resized = resize_framing_rect(
        original,
        bounds,
        "top_left",
        -1.0,
        -0.2,
    )
    scaled = scale_framing_rect(original, bounds, 0.5, 0.3, 0.3)

    assert moved == NormalizedRect(x=0.5, y=0.1, width=0.4, height=0.4)
    assert resized.x == pytest.approx(0.1)
    assert resized.y == pytest.approx(0.1)
    assert resized.width == pytest.approx(0.6)
    assert resized.height == pytest.approx(0.6)
    assert scaled == NormalizedRect(x=0.3, y=0.3, width=0.2, height=0.2)
