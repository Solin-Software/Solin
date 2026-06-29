from __future__ import annotations

from solin.core.projection.image_framing import (
    IMAGE_FRAMING_VERSION,
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    clamp_transform_to_frame,
    constrain_image_transform_for_aspect,
    cover_zoom_for_frame,
    frame_for_aspect,
    image_transform_from_record,
    image_transform_to_record,
    initial_transform_for_frame,
    pan_bounds_for_frame,
    snap_zoom_to_frame_cover,
)


def test_image_framing_record_round_trips_non_identity_transform() -> None:
    transform = ImageTransform(1.4567894, 0.1234567, -0.2345678)

    record = image_transform_to_record(transform)

    assert record == {
        "version": IMAGE_FRAMING_VERSION,
        "zoom": 1.456789,
        "norm_x": 0.123457,
        "norm_y": -0.234568,
    }
    assert image_transform_from_record(record) == ImageTransform(
        1.456789,
        0.123457,
        -0.234568,
    )


def test_image_framing_record_omits_identity_and_rejects_invalid_values() -> None:
    assert image_transform_to_record(IDENTITY_IMAGE_TRANSFORM) is None
    assert image_transform_from_record(None) is None
    assert image_transform_from_record({"version": 99}) is None
    assert image_transform_from_record({
        "version": IMAGE_FRAMING_VERSION,
        "zoom": float("nan"),
        "norm_x": 0.0,
        "norm_y": 0.0,
    }) is None
    assert image_transform_from_record({
        "version": IMAGE_FRAMING_VERSION,
        "zoom": 2.0,
        "norm_x": float("inf"),
        "norm_y": 0.0,
    }) is None


def test_image_framing_record_clamps_zoom_to_renderer_limits() -> None:
    low = image_transform_from_record({
        "version": IMAGE_FRAMING_VERSION,
        "zoom": -10.0,
        "norm_x": 0.1,
        "norm_y": 0.0,
    })
    high = image_transform_from_record({
        "version": IMAGE_FRAMING_VERSION,
        "zoom": 100.0,
        "norm_x": 0.1,
        "norm_y": 0.0,
    })

    assert low is not None and low.zoom == 0.1
    assert high is not None and high.zoom == 10.0


def test_prepared_transform_is_reclamped_when_projection_aspect_changes() -> None:
    authored = constrain_image_transform_for_aspect(
        1000,
        1000,
        16 / 9,
        ImageTransform(1.5, 0.5, -0.5),
    )
    on_four_three = constrain_image_transform_for_aspect(
        1000,
        1000,
        4 / 3,
        authored,
    )
    on_vertical = constrain_image_transform_for_aspect(
        1000,
        1000,
        9 / 16,
        authored,
    )

    assert authored == ImageTransform(1.5, 0.0, -0.25)
    assert on_four_three.norm_x == 0.0
    assert on_four_three.norm_y == -0.25
    assert on_vertical.norm_x == 0.0
    assert on_vertical.norm_y == 0.0


def test_frame_for_aspect_centers_wide_projection_inside_tall_container() -> None:
    frame = frame_for_aspect(1000, 800, 16 / 9)

    assert round(frame.width, 3) == 1000
    assert round(frame.height, 3) == 562.5
    assert frame.x == 0
    assert round(frame.y, 3) == 118.75


def test_frame_for_aspect_centers_tall_projection_inside_wide_container() -> None:
    frame = frame_for_aspect(1400, 800, 16 / 10)

    assert frame.height == 800
    assert frame.width == 1280
    assert frame.x == 60
    assert frame.y == 0


def test_frame_for_aspect_falls_back_to_container_for_invalid_ratio() -> None:
    frame = frame_for_aspect(640, 480, 0)

    assert frame.x == 0
    assert frame.y == 0
    assert frame.width == 640
    assert frame.height == 480


def test_pan_bounds_disable_axis_without_overflow() -> None:
    max_x, max_y = pan_bounds_for_frame(1600, 900, 1000, 1000, 1.0)

    assert max_x == 0.0
    assert max_y == 0.0


def test_pan_bounds_enable_axis_after_user_zoom_creates_overflow() -> None:
    max_x, max_y = pan_bounds_for_frame(1600, 900, 1000, 1000, 1.5)

    assert max_x > 0.0
    assert max_y == 0.0


def test_cover_zoom_returns_exact_threshold_for_tall_image_in_wide_frame() -> None:
    zoom = cover_zoom_for_frame(900, 1600, 1600, 900)

    assert round(zoom, 6) == round(1600 / 506.25, 6)


def test_snap_zoom_lands_on_cover_threshold_once_when_crossing_it() -> None:
    cover_zoom = cover_zoom_for_frame(900, 1600, 1600, 900)

    snapped = snap_zoom_to_frame_cover(3.0, 3.4, 900, 1600, 1600, 900)
    next_step = snap_zoom_to_frame_cover(
        snapped,
        snapped * 1.15,
        900,
        1600,
        1600,
        900,
    )

    assert snapped == cover_zoom
    assert next_step > cover_zoom


def test_snap_zoom_lands_on_cover_threshold_for_wide_image_in_tall_frame() -> None:
    cover_zoom = cover_zoom_for_frame(1600, 900, 900, 1600)

    snapped = snap_zoom_to_frame_cover(3.0, 3.4, 1600, 900, 900, 1600)
    next_step = snap_zoom_to_frame_cover(
        snapped,
        snapped * 1.15,
        1600,
        900,
        900,
        1600,
    )

    assert snapped == cover_zoom
    assert next_step > cover_zoom


def test_constrained_initial_transform_keeps_original_image_fitted() -> None:
    transform = initial_transform_for_frame(
        1600,
        900,
        1000,
        1000,
        constrain_to_frame=True,
    )

    assert transform == IDENTITY_IMAGE_TRANSFORM


def test_unconstrained_initial_transform_stays_identity() -> None:
    transform = initial_transform_for_frame(
        300,
        400,
        1600,
        900,
        constrain_to_frame=False,
    )

    assert transform == IDENTITY_IMAGE_TRANSFORM


def test_clamp_transform_prevents_internal_frame_borders() -> None:
    transform = clamp_transform_to_frame(
        1600,
        900,
        1000,
        1000,
        ImageTransform(1.0, 3.0, -3.0),
    )

    assert transform.zoom == 1.0
    assert transform.norm_x == 0.0
    assert transform.norm_y == 0.0


def test_clamp_transform_allows_pan_within_overflow() -> None:
    transform = clamp_transform_to_frame(
        1600,
        900,
        1000,
        1000,
        ImageTransform(1.5, 0.5, -0.5),
    )

    assert transform.zoom == 1.5
    assert 0 < transform.norm_x < 0.5
    assert transform.norm_y == 0.0
