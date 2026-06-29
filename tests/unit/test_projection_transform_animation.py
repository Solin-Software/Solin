from __future__ import annotations

import pytest

from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
)
from solin.core.projection.transform_animation import (
    PROJECTION_TRANSFORM_DURATION_SECONDS,
    ProjectionTransformAnimation,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_transform_animation_applies_initial_state_instantly() -> None:
    clock = _Clock()
    animation = ProjectionTransformAnimation(clock=clock)
    target = ImageTransform(2.0, 0.25, -0.1)

    assert animation.set_target(target, animate=False) is False
    assert animation.current == target
    assert animation.is_active is False


def test_transform_animation_uses_time_based_premium_easing() -> None:
    clock = _Clock()
    animation = ProjectionTransformAnimation(clock=clock)
    target = ImageTransform(3.0, 0.4, -0.2)

    assert animation.set_target(target, animate=True) is True
    clock.now = PROJECTION_TRANSFORM_DURATION_SECONDS / 2.0
    halfway = animation.sample()

    eased_progress = (halfway.zoom - 1.0) / 2.0
    assert eased_progress == pytest.approx(0.8024, abs=0.001)
    assert halfway.norm_x == pytest.approx(0.4 * eased_progress)
    assert halfway.norm_y == pytest.approx(-0.2 * eased_progress)
    assert animation.is_active is True

    clock.now = PROJECTION_TRANSFORM_DURATION_SECONDS
    assert animation.sample() == target
    assert animation.is_active is False


def test_interrupted_animation_retargets_without_jump_and_uses_ease_out() -> None:
    clock = _Clock()
    animation = ProjectionTransformAnimation(clock=clock)
    animation.set_target(ImageTransform(3.0, 0.4, 0.0), animate=True)
    clock.now = PROJECTION_TRANSFORM_DURATION_SECONDS / 2.0
    before_retarget = animation.sample()

    animation.set_target(ImageTransform(4.0, -0.2, 0.1), animate=True)

    assert animation.current == before_retarget
    clock.now += PROJECTION_TRANSFORM_DURATION_SECONDS * 0.1
    after_retarget = animation.sample()
    expected_progress = (after_retarget.zoom - before_retarget.zoom) / (
        4.0 - before_retarget.zoom
    )
    assert expected_progress == pytest.approx(0.1606, abs=0.001)
    assert after_retarget.zoom == pytest.approx(
        before_retarget.zoom + (4.0 - before_retarget.zoom) * expected_progress
    )
    assert after_retarget.norm_x == pytest.approx(
        before_retarget.norm_x + (-0.2 - before_retarget.norm_x) * expected_progress
    )


def test_transform_animation_reset_cancels_active_motion() -> None:
    clock = _Clock()
    animation = ProjectionTransformAnimation(clock=clock)
    animation.set_target(ImageTransform(2.0, 0.2, 0.1), animate=True)

    animation.reset()

    assert animation.current == IDENTITY_IMAGE_TRANSFORM
    assert animation.target == IDENTITY_IMAGE_TRANSFORM
    assert animation.is_active is False


def test_duplicate_target_does_not_restart_active_animation() -> None:
    clock = _Clock()
    animation = ProjectionTransformAnimation(clock=clock)
    target = ImageTransform(2.0, 0.2, 0.1)
    animation.set_target(target, animate=True)
    clock.now = PROJECTION_TRANSFORM_DURATION_SECONDS / 2.0

    assert animation.set_target(target, animate=True) is True
    clock.now = PROJECTION_TRANSFORM_DURATION_SECONDS

    assert animation.sample() == target
    assert animation.is_active is False
