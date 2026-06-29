from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


IMAGE_FRAMING_VERSION = 1
MIN_IMAGE_ZOOM = 0.1
MAX_IMAGE_ZOOM = 10.0
_IDENTITY_EPSILON = 1e-6
_SERIALIZED_PRECISION = 6


@dataclass(frozen=True, slots=True)
class FramingRect:
    x: float
    y: float
    width: float
    height: float

    @property
    def is_empty(self) -> bool:
        return self.width <= 0.0 or self.height <= 0.0


@dataclass(frozen=True, slots=True)
class ImageTransform:
    zoom: float
    norm_x: float
    norm_y: float


IDENTITY_IMAGE_TRANSFORM = ImageTransform(1.0, 0.0, 0.0)


def image_transform_from_record(value: object) -> ImageTransform | None:
    """Parse a persisted prepared-image framing record.

    Invalid or identity records are treated as absent so legacy items and
    corrupted optional metadata always retain the original fit-to-frame path.
    """

    if not isinstance(value, dict):
        return None
    if value.get("version") != IMAGE_FRAMING_VERSION:
        return None
    zoom = _finite_float(value.get("zoom"))
    norm_x = _finite_float(value.get("norm_x"))
    norm_y = _finite_float(value.get("norm_y"))
    if zoom is None or norm_x is None or norm_y is None:
        return None
    transform = normalize_image_transform(ImageTransform(zoom, norm_x, norm_y))
    return None if is_identity_image_transform(transform) else transform


def image_transform_to_record(transform: ImageTransform | None) -> dict[str, Any] | None:
    """Return the stable JSON record for a non-identity image transform."""

    if transform is None:
        return None
    normalized = normalize_image_transform(transform)
    if is_identity_image_transform(normalized):
        return None
    return {
        "version": IMAGE_FRAMING_VERSION,
        "zoom": round(normalized.zoom, _SERIALIZED_PRECISION),
        "norm_x": round(normalized.norm_x, _SERIALIZED_PRECISION),
        "norm_y": round(normalized.norm_y, _SERIALIZED_PRECISION),
    }


def normalize_image_transform(transform: ImageTransform) -> ImageTransform:
    """Clamp finite transform values to renderer-supported zoom limits."""

    zoom = _finite_float(transform.zoom)
    norm_x = _finite_float(transform.norm_x)
    norm_y = _finite_float(transform.norm_y)
    if zoom is None or norm_x is None or norm_y is None:
        return IDENTITY_IMAGE_TRANSFORM
    return ImageTransform(
        _clamp(zoom, MIN_IMAGE_ZOOM, MAX_IMAGE_ZOOM),
        norm_x,
        norm_y,
    )


def is_identity_image_transform(transform: ImageTransform) -> bool:
    normalized = normalize_image_transform(transform)
    return (
        abs(normalized.zoom - 1.0) <= _IDENTITY_EPSILON
        and abs(normalized.norm_x) <= _IDENTITY_EPSILON
        and abs(normalized.norm_y) <= _IDENTITY_EPSILON
    )


def constrain_image_transform(
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
    transform: ImageTransform,
) -> ImageTransform:
    """Validate and clamp a prepared transform using the strict thumb policy."""

    return clamp_transform_to_frame(
        image_width,
        image_height,
        frame_width,
        frame_height,
        normalize_image_transform(transform),
    )


def constrain_image_transform_for_aspect(
    image_width: float,
    image_height: float,
    aspect_ratio: float,
    transform: ImageTransform,
) -> ImageTransform:
    """Clamp a transform against a normalized frame with *aspect_ratio*."""

    ratio = _finite_float(aspect_ratio)
    if ratio is None or ratio <= 0.0:
        ratio = 16.0 / 9.0
    return constrain_image_transform(
        image_width,
        image_height,
        ratio,
        1.0,
        transform,
    )


def prepare_image_transform_for_aspect(
    image_width: float,
    image_height: float,
    aspect_ratio: float,
    previous_transform: ImageTransform | None,
    requested_transform: ImageTransform,
) -> ImageTransform:
    """Snap a zoom crossing to frame coverage, then clamp the full transform."""

    ratio = _finite_float(aspect_ratio)
    if ratio is None or ratio <= 0.0:
        ratio = 16.0 / 9.0
    previous = normalize_image_transform(
        previous_transform or IDENTITY_IMAGE_TRANSFORM
    )
    requested = normalize_image_transform(requested_transform)
    snapped_zoom = snap_zoom_to_frame_cover(
        previous.zoom,
        requested.zoom,
        image_width,
        image_height,
        ratio,
        1.0,
    )
    return constrain_image_transform(
        image_width,
        image_height,
        ratio,
        1.0,
        ImageTransform(snapped_zoom, requested.norm_x, requested.norm_y),
    )


def frame_for_aspect(
    container_width: float,
    container_height: float,
    aspect_ratio: float,
) -> FramingRect:
    """Return the largest centered rect matching aspect_ratio inside a container."""

    container_width = max(0.0, float(container_width))
    container_height = max(0.0, float(container_height))
    aspect_ratio = float(aspect_ratio) if aspect_ratio else 0.0
    if container_width <= 0.0 or container_height <= 0.0 or aspect_ratio <= 0.0:
        return FramingRect(0.0, 0.0, container_width, container_height)

    container_ratio = container_width / container_height
    if container_ratio > aspect_ratio:
        height = container_height
        width = height * aspect_ratio
    else:
        width = container_width
        height = width / aspect_ratio
    return FramingRect(
        (container_width - width) / 2.0,
        (container_height - height) / 2.0,
        width,
        height,
    )


def pan_bounds_for_frame(
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
    zoom: float,
) -> tuple[float, float]:
    """Return max normalized pan per axis without adding avoidable frame gaps."""

    image_width = float(image_width)
    image_height = float(image_height)
    frame_width = float(frame_width)
    frame_height = float(frame_height)
    zoom = max(MIN_IMAGE_ZOOM, float(zoom))
    if (
        image_width <= 0.0
        or image_height <= 0.0
        or frame_width <= 0.0
        or frame_height <= 0.0
    ):
        return (0.0, 0.0)

    fit_scale = min(frame_width / image_width, frame_height / image_height)
    drawn_width = image_width * fit_scale * zoom
    drawn_height = image_height * fit_scale * zoom
    max_x = max(0.0, (drawn_width - frame_width) / (2.0 * frame_width))
    max_y = max(0.0, (drawn_height - frame_height) / (2.0 * frame_height))
    return (max_x, max_y)


def cover_zoom_for_frame(
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
) -> float:
    """Return the zoom where a fitted image first covers the whole frame."""

    image_width = float(image_width)
    image_height = float(image_height)
    frame_width = float(frame_width)
    frame_height = float(frame_height)
    if (
        image_width <= 0.0
        or image_height <= 0.0
        or frame_width <= 0.0
        or frame_height <= 0.0
    ):
        return 1.0

    fit_scale = min(frame_width / image_width, frame_height / image_height)
    if fit_scale <= 0.0:
        return 1.0

    fitted_width = image_width * fit_scale
    fitted_height = image_height * fit_scale
    return max(frame_width / fitted_width, frame_height / fitted_height)


def snap_zoom_to_frame_cover(
    previous_zoom: float,
    requested_zoom: float,
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
) -> float:
    """Snap a zoom-in step to the exact frame-cover threshold when it crosses it."""

    previous_zoom = max(MIN_IMAGE_ZOOM, float(previous_zoom))
    requested_zoom = max(MIN_IMAGE_ZOOM, float(requested_zoom))
    if requested_zoom <= previous_zoom:
        return requested_zoom

    cover_zoom = cover_zoom_for_frame(
        image_width,
        image_height,
        frame_width,
        frame_height,
    )
    if previous_zoom < cover_zoom < requested_zoom:
        return cover_zoom
    return requested_zoom


def clamp_transform_to_frame(
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
    transform: ImageTransform,
) -> ImageTransform:
    """Clamp transform so the drawn image fully covers the target frame."""

    image_width = float(image_width)
    image_height = float(image_height)
    frame_width = float(frame_width)
    frame_height = float(frame_height)
    if (
        image_width <= 0.0
        or image_height <= 0.0
        or frame_width <= 0.0
        or frame_height <= 0.0
    ):
        return IDENTITY_IMAGE_TRANSFORM

    zoom = _clamp(float(transform.zoom), MIN_IMAGE_ZOOM, MAX_IMAGE_ZOOM)
    max_x, max_y = pan_bounds_for_frame(
        image_width,
        image_height,
        frame_width,
        frame_height,
        zoom,
    )
    return ImageTransform(
        zoom,
        _clamp(float(transform.norm_x), -max_x, max_x),
        _clamp(float(transform.norm_y), -max_y, max_y),
    )


def initial_transform_for_frame(
    image_width: float,
    image_height: float,
    frame_width: float,
    frame_height: float,
    *,
    constrain_to_frame: bool,
) -> ImageTransform:
    return IDENTITY_IMAGE_TRANSFORM


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
