from __future__ import annotations

from dataclasses import dataclass


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
    zoom = max(0.1, float(zoom))
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

    zoom = max(0.1, float(transform.zoom))
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
