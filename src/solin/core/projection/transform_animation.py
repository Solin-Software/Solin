"""Time-based, retargetable animation for projected image transforms."""

from __future__ import annotations

from collections.abc import Callable
import time

from .image_framing import IDENTITY_IMAGE_TRANSFORM, ImageTransform


PROJECTION_TRANSFORM_DURATION_SECONDS = 2.1
_CSS_EASE = (0.25, 0.1, 0.25, 1.0)
_CSS_EASE_OUT = (0.0, 0.0, 0.58, 1.0)


class ProjectionTransformAnimation:
    """Interpolate zoom/pan without frame-rate-dependent motion or jumps."""

    def __init__(
        self,
        *,
        duration_seconds: float = PROJECTION_TRANSFORM_DURATION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if duration_seconds <= 0.0:
            raise ValueError("Animation duration must be positive")
        self._duration_seconds = float(duration_seconds)
        self._clock = clock
        self._current = IDENTITY_IMAGE_TRANSFORM
        self._start = IDENTITY_IMAGE_TRANSFORM
        self._target = IDENTITY_IMAGE_TRANSFORM
        self._started_at: float | None = None
        self._interrupted = False

    @property
    def current(self) -> ImageTransform:
        return self._current

    @property
    def target(self) -> ImageTransform:
        return self._target

    @property
    def is_active(self) -> bool:
        return self._started_at is not None

    def set_target(
        self,
        transform: ImageTransform,
        *,
        animate: bool,
    ) -> bool:
        """Retarget from the exact current position and return active state."""

        now = self._clock()
        if self.is_active:
            self._sample_at(now)
        if animate and self.is_active and transform == self._target:
            return True
        interrupted = self.is_active

        self._target = transform
        if not animate or transform == self._current:
            self._current = transform
            self._start = transform
            self._started_at = None
            self._interrupted = False
            return False

        self._start = self._current
        self._started_at = now
        self._interrupted = interrupted
        return True

    def reset(self) -> None:
        self.set_target(IDENTITY_IMAGE_TRANSFORM, animate=False)

    def sample(self) -> ImageTransform:
        if self.is_active:
            self._sample_at(self._clock())
        return self._current

    def _sample_at(self, now: float) -> None:
        if self._started_at is None:
            return
        elapsed = max(0.0, now - self._started_at)
        progress = min(1.0, elapsed / self._duration_seconds)
        eased = (
            _cubic_bezier_ease(progress, *_CSS_EASE_OUT)
            if self._interrupted
            else _cubic_bezier_ease(progress, *_CSS_EASE)
        )
        self._current = _interpolate_transform(self._start, self._target, eased)
        if progress >= 1.0:
            self._current = self._target
            self._started_at = None
            self._interrupted = False


def _cubic_bezier_ease(
    progress: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Evaluate a CSS-style cubic-bezier easing at normalized time."""

    if progress <= 0.0:
        return 0.0
    if progress >= 1.0:
        return 1.0

    low = 0.0
    high = 1.0
    for _iteration in range(20):
        parameter = (low + high) / 2.0
        x = _cubic_bezier_coordinate(parameter, x1, x2)
        if x < progress:
            low = parameter
        else:
            high = parameter
    return _cubic_bezier_coordinate((low + high) / 2.0, y1, y2)


def _cubic_bezier_coordinate(
    parameter: float,
    control_1: float,
    control_2: float,
) -> float:
    inverse = 1.0 - parameter
    return (
        3.0 * inverse * inverse * parameter * control_1
        + 3.0 * inverse * parameter * parameter * control_2
        + parameter * parameter * parameter
    )


def _interpolate_transform(
    start: ImageTransform,
    target: ImageTransform,
    progress: float,
) -> ImageTransform:
    return ImageTransform(
        start.zoom + (target.zoom - start.zoom) * progress,
        start.norm_x + (target.norm_x - start.norm_x) * progress,
        start.norm_y + (target.norm_y - start.norm_y) * progress,
    )
