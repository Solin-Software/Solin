from __future__ import annotations

from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    clamp_transform_to_frame,
    frame_for_aspect,
    initial_transform_for_frame,
    pan_bounds_for_frame,
)


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
