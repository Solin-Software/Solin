from __future__ import annotations

import pytest

from solin.core.scenes.editor_geometry import (
    crop_for_framing_rect,
    crop_geometry,
    expand_focus_rect_to_aspect,
    fill_crop_to_canvas,
    move_framing_rect,
    move_rect,
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


def test_editor_move_allows_negative_position_but_keeps_a_sliver_on_canvas() -> None:
    original = NormalizedRect(x=0.1, y=0.2, width=0.5, height=0.4)

    moved = move_rect(original, -0.3, -0.5)
    assert moved.x == pytest.approx(-0.2)
    assert moved.y == pytest.approx(-0.3)
    assert (moved.width, moved.height) == (original.width, original.height)

    far_left = move_rect(original, -5.0, -5.0)
    assert far_left.x == pytest.approx(0.02 - original.width)
    assert far_left.y == pytest.approx(0.02 - original.height)

    far_right = move_rect(original, 5.0, 5.0)
    assert far_right.x == pytest.approx(0.98)
    assert far_right.y == pytest.approx(0.98)


def test_editor_move_does_not_jump_a_layer_already_past_the_limits() -> None:
    original = NormalizedRect(x=-1.5, y=-1.5, width=0.5, height=0.5)

    assert move_rect(original, -0.1, -0.1) == original
    assert move_rect(original, 0.1, 0.1).x == pytest.approx(-1.4)


def test_editor_resize_keeps_off_canvas_edges_in_place() -> None:
    original = NormalizedRect(x=-0.2, y=-0.1, width=0.5, height=0.4)

    free = resize_rect(original, "bottom_right", 0.1, 0.1, preserve_aspect=False)
    assert (free.x, free.y) == (original.x, original.y)
    assert free.width == pytest.approx(0.6)
    assert free.height == pytest.approx(0.5)

    shrunk = resize_rect(original, "top_left", 0.05, 0.05, preserve_aspect=False)
    assert shrunk.x == pytest.approx(-0.15)
    assert shrunk.y == pytest.approx(-0.05)
    assert shrunk.x + shrunk.width == pytest.approx(0.3)

    grown_outward = resize_rect(original, "left", -0.3, 0.0, preserve_aspect=False)
    assert grown_outward.x == pytest.approx(original.x)

    preserved = resize_rect(original, "bottom_right", 0.1, 0.0, preserve_aspect=True)
    assert (preserved.x, preserved.y) == (original.x, original.y)
    assert preserved.width == pytest.approx(0.6)
    assert preserved.width / preserved.height == pytest.approx(
        original.width / original.height
    )


def test_editor_crop_of_an_off_canvas_layer_does_not_force_a_crop() -> None:
    original = NormalizedRect(x=-0.2, y=-0.1, width=0.5, height=0.4)

    rect, crop = crop_geometry(original, Crop(), "top_left", 0.0, 0.0)
    assert rect == original
    assert crop == Crop()

    rect, crop = crop_geometry(original, Crop(), "top_left", 0.1, 0.1)
    assert rect.x == pytest.approx(-0.1)
    assert crop.left == pytest.approx(0.2)
    assert crop.top == pytest.approx(0.25)

    beyond = NormalizedRect(x=0.7, y=0.8, width=0.5, height=0.4)
    rect, crop = crop_geometry(beyond, Crop(), "bottom_right", 0.0, 0.0)
    assert (rect.x, rect.y, rect.width, rect.height) == pytest.approx(
        (beyond.x, beyond.y, beyond.width, beyond.height)
    )
    assert crop == Crop()


@pytest.mark.parametrize("preserve_aspect", [False, True])
@pytest.mark.parametrize("handle", ["left", "top", "top_left"])
@pytest.mark.parametrize("delta", [-10.0, 10.0])
def test_resize_respects_model_limits_and_keeps_the_opposite_edges(
    preserve_aspect: bool, handle: str, delta: float,
) -> None:
    original = NormalizedRect(x=1.5, y=1.5, width=3.0, height=3.0)

    resized = resize_rect(original, handle, delta, delta, preserve_aspect=preserve_aspect)

    assert -2.0 <= resized.x <= 2.0
    assert -2.0 <= resized.y <= 2.0
    assert 0.001 <= resized.width <= 4.0
    assert 0.001 <= resized.height <= 4.0
    if "left" in handle:
        assert resized.x + resized.width == pytest.approx(original.x + original.width)
    if "top" in handle:
        assert resized.y + resized.height == pytest.approx(original.y + original.height)
    if preserve_aspect:
        assert resized.width / resized.height == pytest.approx(original.width / original.height)


@pytest.mark.parametrize("handle", ["left", "top", "top_left", "bottom_right"])
@pytest.mark.parametrize("preserve_aspect", [False, True])
def test_resize_does_not_enlarge_valid_layers_smaller_than_editor_minimum(
    handle: str, preserve_aspect: bool,
) -> None:
    original = NormalizedRect(x=-0.0005, y=0.9995, width=0.001, height=0.002)

    assert resize_rect(original, handle, 0.0, 0.0, preserve_aspect=preserve_aspect) == original


@pytest.mark.parametrize("handle", ["left", "top", "top_left"])
@pytest.mark.parametrize("delta", [-10.0, 10.0])
@pytest.mark.parametrize("size", [3.0, 4.0])
def test_crop_respects_model_limits_without_rescaling_the_source(
    handle: str, delta: float, size: float,
) -> None:
    original = NormalizedRect(x=1.5, y=1.5, width=size, height=size)
    original_crop = Crop(left=0.2, top=0.2)

    rect, crop = crop_geometry(original, original_crop, handle, delta, delta)

    assert NormalizedRect.MINIMUM_POSITION <= rect.x <= NormalizedRect.MAXIMUM_POSITION
    assert NormalizedRect.MINIMUM_POSITION <= rect.y <= NormalizedRect.MAXIMUM_POSITION
    assert NormalizedRect.MINIMUM_SIZE <= rect.width <= NormalizedRect.MAXIMUM_SIZE
    assert NormalizedRect.MINIMUM_SIZE <= rect.height <= NormalizedRect.MAXIMUM_SIZE
    assert rect.x + rect.width == pytest.approx(original.x + original.width)
    assert rect.y + rect.height == pytest.approx(original.y + original.height)
    assert rect.width / (1.0 - crop.left - crop.right) == pytest.approx(original.width / 0.8)
    assert rect.height / (1.0 - crop.top - crop.bottom) == pytest.approx(original.height / 0.8)


@pytest.mark.parametrize("handle", ["left", "right", "top", "bottom", "top_left", "bottom_right"])
def test_crop_does_not_change_valid_tiny_or_heavily_cropped_layers_on_grab(handle: str) -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=0.001, height=0.002)
    original_crop = Crop(left=0.99, right=0.009, top=0.99, bottom=0.009)

    rect, crop = crop_geometry(original, original_crop, handle, 0.0, 0.0)

    assert rect.to_record() == pytest.approx(original.to_record())
    assert crop == original_crop


@pytest.mark.parametrize("handle", ["left", "right", "top", "bottom", "top_left", "bottom_right"])
@pytest.mark.parametrize("delta", [-0.2, 0.2])
@pytest.mark.parametrize("leading_crop", [0.01, 0.4, 0.6])
def test_extreme_crop_rounding_preserves_fixed_edges_and_source_scale(
    handle: str, delta: float, leading_crop: float,
) -> None:
    original = NormalizedRect(x=0.2, y=0.2, width=3.8, height=3.8)
    trailing_crop = 1.0 - leading_crop - 1.5e-15
    original_crop = Crop(left=leading_crop, right=trailing_crop,
                         top=leading_crop, bottom=trailing_crop)

    rect, crop = crop_geometry(original, original_crop, handle, delta, delta)

    if "left" in handle:
        assert rect.x + rect.width == pytest.approx(original.x + original.width)
    else:
        assert rect.x == original.x
    if "top" in handle:
        assert rect.y + rect.height == pytest.approx(original.y + original.height)
    else:
        assert rect.y == original.y
    assert rect.width / (1.0 - crop.left - crop.right) == pytest.approx(
        original.width / (1.0 - original_crop.left - original_crop.right),
    )
    assert rect.height / (1.0 - crop.top - crop.bottom) == pytest.approx(
        original.height / (1.0 - original_crop.top - original_crop.bottom),
    )


def test_uncropping_at_zero_handles_roundoff_without_walking_subnormal_values() -> None:
    original = NormalizedRect(x=0.04285714285714286, y=0.2, width=0.1, height=0.5)
    original_crop = Crop(left=0.3)

    rect, crop = crop_geometry(original, original_crop, "left", -0.1, 0.0)

    assert 0.0 <= rect.x < original.x
    assert crop.left < original_crop.left
    assert rect.x + rect.width == pytest.approx(original.x + original.width)
    assert rect.width / (1.0 - crop.left - crop.right) == pytest.approx(original.width / 0.7)
