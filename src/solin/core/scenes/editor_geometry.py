"""Pure editor geometry independent from QWidget, QML, and the renderer."""

from __future__ import annotations

import math
from dataclasses import dataclass

from solin.core.scenes.model import MAXIMUM_CROP_FRACTION, Crop, FitMode, NormalizedRect, SceneLayer


HANDLE_NAMES = (
    "top_left",
    "top",
    "top_right",
    "right",
    "bottom_right",
    "bottom",
    "bottom_left",
    "left",
)
MINIMUM_LAYER_SIZE = 0.02
MINIMUM_VISIBLE_SOURCE = 0.01
SNAP_THRESHOLD_PIXELS = 8.0
SNAP_RELEASE_THRESHOLD_PIXELS = 14.0
EDGE_SNAP_MARGIN_SHORT_EDGE = 0.05
# Rect coordinates are normalized independently on each canvas axis, so a
# square in this space always has the configured canvas's physical aspect.
NORMALIZED_CANVAS_ASPECT = 1.0
_GEOMETRY_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class FramingRect:
    """Temporary source coordinates, independent of persisted layer size limits."""

    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (self.x, self.y, self.width, self.height)
        ) or self.width <= 0 or self.height <= 0:
            raise ValueError("Framing geometry must be finite with positive dimensions")


def intersect_rect(left: NormalizedRect, right: NormalizedRect) -> NormalizedRect | None:
    x = max(left.x, right.x)
    y = max(left.y, right.y)
    right_edge = min(left.x + left.width, right.x + right.width)
    bottom_edge = min(left.y + left.height, right.y + right.height)
    width = right_edge - x
    height = bottom_edge - y
    if width <= _GEOMETRY_EPSILON or height <= _GEOMETRY_EPSILON:
        return None
    return NormalizedRect(x=x, y=y, width=width, height=height)


def inscribed_aspect_rect[RectT: (NormalizedRect, FramingRect)](
    bounds: RectT,
    *,
    aspect: float = NORMALIZED_CANVAS_ASPECT,
) -> RectT:
    """Return the largest centered aspect rectangle inside ``bounds``.

    The aspect is expressed in normalized canvas coordinates. An aspect of 1
    therefore matches the canvas regardless of its physical output format.
    """

    _require_aspect(aspect)
    if bounds.width / bounds.height > aspect:
        height = bounds.height
        width = height * aspect
    else:
        width = bounds.width
        height = width / aspect
    return type(bounds)(
        x=bounds.x + (bounds.width - width) / 2.0,
        y=bounds.y + (bounds.height - height) / 2.0,
        width=width,
        height=height,
    )


def recover_uncropped_rect(
    rect: NormalizedRect,
    crop: Crop,
    *,
    mirror_x: bool = False,
    mirror_y: bool = False,
) -> NormalizedRect:
    """Recover the source footprint represented by an editor crop operation."""

    visible_x = 1.0 - crop.left - crop.right
    visible_y = 1.0 - crop.top - crop.bottom
    visual_left = crop.right if mirror_x else crop.left
    visual_top = crop.bottom if mirror_y else crop.top
    width = rect.width / visible_x
    height = rect.height / visible_y
    return NormalizedRect(
        x=_clean_geometry_value(rect.x - visual_left * width),
        y=_clean_geometry_value(rect.y - visual_top * height),
        width=_clean_geometry_value(width),
        height=_clean_geometry_value(height),
    )


def source_framing_geometry(
    layer: SceneLayer,
    *,
    source_width: int,
    source_height: int,
    canvas_width: int,
    canvas_height: int,
) -> tuple[FramingRect, FramingRect]:
    """Place the full source and its current visible focus in the editor.

    Coordinates use the output canvas axes. The source keeps its physical
    aspect; the proposed focus fits the output aspect inside the current crop.
    COVER's implicit clipping is included before proposing the output focus.
    """
    dimensions = (source_width, source_height, canvas_width, canvas_height)
    if any(type(value) is not int or value <= 0 for value in dimensions):
        raise ValueError("Framing requires positive source and canvas dimensions")
    bounds = inscribed_aspect_rect(
        FramingRect(),
        aspect=(source_width / source_height) / (canvas_width / canvas_height),
    )
    crop = layer.crop
    focus = FramingRect(
        x=bounds.x + (crop.right if layer.mirror_x else crop.left) * bounds.width,
        y=bounds.y + (crop.bottom if layer.mirror_y else crop.top) * bounds.height,
        width=(1.0 - crop.left - crop.right) * bounds.width,
        height=(1.0 - crop.top - crop.bottom) * bounds.height,
    )
    if layer.fit_mode is FitMode.COVER:
        focus = inscribed_aspect_rect(focus, aspect=layer.rect.width / layer.rect.height)
    return bounds, inscribed_aspect_rect(focus)


def expand_focus_rect_to_aspect(
    focus: NormalizedRect,
    bounds: NormalizedRect,
    *,
    aspect: float = NORMALIZED_CANVAS_ASPECT,
) -> NormalizedRect:
    """Fit a centered focus region to an aspect, expanding before shrinking.

    The function first tries to retain the complete focus region by expanding
    its deficient axis. If the source boundary prevents that expansion while
    preserving the focus center, both target dimensions are scaled down until
    the aspect rectangle fits.
    """

    _require_aspect(aspect)
    center_x = focus.x + focus.width / 2.0
    center_y = focus.y + focus.height / 2.0
    if not (
        bounds.x - _GEOMETRY_EPSILON
        <= focus.x
        <= focus.x + focus.width
        <= bounds.x + bounds.width + _GEOMETRY_EPSILON
        and bounds.y - _GEOMETRY_EPSILON
        <= focus.y
        <= focus.y + focus.height
        <= bounds.y + bounds.height + _GEOMETRY_EPSILON
    ):
        raise ValueError("Focus rectangle must be contained by its source bounds")

    if focus.width / focus.height < aspect:
        desired_width = focus.height * aspect
        desired_height = focus.height
    else:
        desired_width = focus.width
        desired_height = focus.width / aspect

    maximum_width = 2.0 * min(
        center_x - bounds.x,
        bounds.x + bounds.width - center_x,
    )
    maximum_height = 2.0 * min(
        center_y - bounds.y,
        bounds.y + bounds.height - center_y,
    )
    scale = min(
        1.0,
        maximum_width / desired_width,
        maximum_height / desired_height,
    )
    width = desired_width * scale
    height = desired_height * scale
    return NormalizedRect(
        x=center_x - width / 2.0,
        y=center_y - height / 2.0,
        width=width,
        height=height,
    )


def crop_for_framing_rect(
    source_rect: NormalizedRect | FramingRect,
    source_crop: Crop,
    frame: NormalizedRect | FramingRect,
    *,
    mirror_x: bool = False,
    mirror_y: bool = False,
) -> Crop:
    """Map a visible editor frame back into source-relative crop values."""

    relative_left = (frame.x - source_rect.x) / source_rect.width
    relative_top = (frame.y - source_rect.y) / source_rect.height
    relative_right = (frame.x + frame.width - source_rect.x) / source_rect.width
    relative_bottom = (frame.y + frame.height - source_rect.y) / source_rect.height
    for value in (relative_left, relative_top, relative_right, relative_bottom):
        if not -_GEOMETRY_EPSILON <= value <= 1.0 + _GEOMETRY_EPSILON:
            raise ValueError("Framing rectangle must stay inside the visible source")

    relative_left = min(1.0, max(0.0, relative_left))
    relative_top = min(1.0, max(0.0, relative_top))
    relative_right = min(1.0, max(0.0, relative_right))
    relative_bottom = min(1.0, max(0.0, relative_bottom))
    visible_x = 1.0 - source_crop.left - source_crop.right
    visible_y = 1.0 - source_crop.top - source_crop.bottom
    if mirror_x:
        source_left = source_crop.left + (1.0 - relative_right) * visible_x
        source_right_edge = source_crop.left + (1.0 - relative_left) * visible_x
    else:
        source_left = source_crop.left + relative_left * visible_x
        source_right_edge = source_crop.left + relative_right * visible_x
    if mirror_y:
        source_top = source_crop.top + (1.0 - relative_bottom) * visible_y
        source_bottom_edge = source_crop.top + (1.0 - relative_top) * visible_y
    else:
        source_top = source_crop.top + relative_top * visible_y
        source_bottom_edge = source_crop.top + relative_bottom * visible_y
    return Crop(
        left=_clean_crop_value(source_left),
        top=_clean_crop_value(source_top),
        right=_clean_crop_value(1.0 - source_right_edge),
        bottom=_clean_crop_value(1.0 - source_bottom_edge),
    )


def fill_crop_to_canvas(
    rect: NormalizedRect,
    crop: Crop,
    *,
    mirror_x: bool = False,
    mirror_y: bool = False,
    aspect: float = NORMALIZED_CANVAS_ASPECT,
) -> Crop:
    """Expand a crop to the canvas aspect when possible, otherwise tighten it."""

    _require_aspect(aspect)
    if abs(rect.width / rect.height - aspect) <= _GEOMETRY_EPSILON:
        return crop
    source_bounds = recover_uncropped_rect(
        rect,
        crop,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )
    focus = expand_focus_rect_to_aspect(rect, source_bounds, aspect=aspect)
    return crop_for_framing_rect(
        source_bounds,
        Crop(),
        focus,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )


def move_framing_rect[RectT: (NormalizedRect, FramingRect)](
    rect: RectT,
    bounds: RectT,
    dx: float,
    dy: float,
) -> RectT:
    # Tiny authored crops can be narrower than the per-edge crop limit. Keep
    # both edges representable while preserving the existing selection size.
    edge_visibility = 1.0 - MAXIMUM_CROP_FRACTION
    minimum_x = max(bounds.x, bounds.x + edge_visibility * bounds.width - rect.width)
    maximum_x = min(bounds.x + bounds.width - rect.width,
                    bounds.x + MAXIMUM_CROP_FRACTION * bounds.width)
    minimum_y = max(bounds.y, bounds.y + edge_visibility * bounds.height - rect.height)
    maximum_y = min(bounds.y + bounds.height - rect.height,
                    bounds.y + MAXIMUM_CROP_FRACTION * bounds.height)
    return type(rect)(
        x=min(maximum_x, max(minimum_x, rect.x + dx)),
        y=min(maximum_y, max(minimum_y, rect.y + dy)),
        width=rect.width,
        height=rect.height,
    )


def resize_framing_rect[RectT: (NormalizedRect, FramingRect)](
    rect: RectT,
    bounds: RectT,
    handle: str,
    dx: float,
    dy: float,
    *,
    aspect: float = NORMALIZED_CANVAS_ASPECT,
    minimum_width: float = MINIMUM_LAYER_SIZE,
) -> RectT:
    if handle not in {"top_left", "top_right", "bottom_right", "bottom_left"}:
        raise ValueError(f"Unknown framing handle: {handle}")
    _require_aspect(aspect)
    moves_left = "left" in handle
    moves_top = "top" in handle
    fixed_x = rect.x + rect.width if moves_left else rect.x
    fixed_y = rect.y + rect.height if moves_top else rect.y
    width_from_x = rect.width - dx if moves_left else rect.width + dx
    height_from_y = rect.height - dy if moves_top else rect.height + dy
    width_from_y = height_from_y * aspect
    width = (
        width_from_x
        if abs(width_from_x - rect.width) >= abs(width_from_y - rect.width)
        else width_from_y
    )
    maximum_width = min(
        fixed_x - bounds.x if moves_left else bounds.x + bounds.width - fixed_x,
        (fixed_y - bounds.y if moves_top else bounds.y + bounds.height - fixed_y) * aspect,
    )
    minimum_width = max(minimum_width, MINIMUM_LAYER_SIZE * aspect)
    width = min(maximum_width, max(minimum_width, width))
    height = width / aspect
    resized = type(rect)(
        x=fixed_x - width if moves_left else fixed_x,
        y=fixed_y - height if moves_top else fixed_y,
        width=width,
        height=height,
    )
    return move_framing_rect(resized, bounds, 0.0, 0.0)


def scale_framing_rect[RectT: (NormalizedRect, FramingRect)](
    rect: RectT,
    bounds: RectT,
    scale: float,
    anchor_x: float,
    anchor_y: float,
    *,
    aspect: float = NORMALIZED_CANVAS_ASPECT,
    minimum_width: float = MINIMUM_LAYER_SIZE,
) -> RectT:
    _require_aspect(aspect)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("Framing scale must be finite and positive")
    maximum_width = min(bounds.width, bounds.height * aspect)
    minimum_width = max(minimum_width, MINIMUM_LAYER_SIZE * aspect)
    width = min(maximum_width, max(minimum_width, rect.width * scale))
    height = width / aspect
    relative_x = min(1.0, max(0.0, (anchor_x - rect.x) / rect.width))
    relative_y = min(1.0, max(0.0, (anchor_y - rect.y) / rect.height))
    x = anchor_x - relative_x * width
    y = anchor_y - relative_y * height
    scaled = type(rect)(
        x=x,
        y=y,
        width=width,
        height=height,
    )
    return move_framing_rect(scaled, bounds, 0.0, 0.0)


def _require_aspect(aspect: float) -> None:
    if not math.isfinite(aspect) or aspect <= 0.0:
        raise ValueError("Framing aspect must be finite and positive")


def _clean_crop_value(value: float) -> float:
    if abs(value) <= _GEOMETRY_EPSILON:
        return 0.0
    if abs(value - 1.0) <= _GEOMETRY_EPSILON:
        return 1.0
    if abs(value - MAXIMUM_CROP_FRACTION) <= _GEOMETRY_EPSILON:
        return MAXIMUM_CROP_FRACTION
    return min(1.0, max(0.0, value))


def _clean_geometry_value(value: float) -> float:
    if abs(value) <= _GEOMETRY_EPSILON:
        return 0.0
    if abs(value - 1.0) <= _GEOMETRY_EPSILON:
        return 1.0
    return value


def snap_move_rect(
    rect: NormalizedRect,
    *,
    viewport_width: float,
    viewport_height: float,
    disabled: bool = False,
    active_guides: tuple[tuple[str, float], ...] = (),
) -> tuple[NormalizedRect, tuple[tuple[str, float], ...]]:
    if disabled or viewport_width <= 0 or viewport_height <= 0:
        return rect, ()
    active_by_axis = dict(active_guides)
    safe_margin_pixels = min(viewport_width, viewport_height) * EDGE_SNAP_MARGIN_SHORT_EDGE

    def snap_axis(
        axis: str,
        start: float,
        size: float,
        viewport: float,
    ) -> tuple[float, float | None]:
        safe_margin = safe_margin_pixels / viewport
        candidates = (
            (start, 0.0, safe_margin),
            (start, 0.0, 0.5),
            (start + size / 2.0, size / 2.0, 0.5),
            (start + size, size, 0.5),
            (start + size, size, 1.0 - safe_margin),
        )
        best: tuple[float, float] | None = None
        best_distance = float("inf")
        active_target = active_by_axis.get(axis)
        if active_target is not None:
            for point, offset, target in candidates:
                if target != active_target:
                    continue
                candidate = target - offset
                distance = abs(point - target) * viewport
                if (
                    0.0 <= candidate <= 1.0 - size
                    and distance <= SNAP_RELEASE_THRESHOLD_PIXELS
                    and distance < best_distance
                ):
                    best = (candidate, target)
                    best_distance = distance
            if best is not None:
                return best
        for point, offset, target in candidates:
            candidate = target - offset
            distance = abs(point - target) * viewport
            if (
                0.0 <= candidate <= 1.0 - size
                and distance <= SNAP_THRESHOLD_PIXELS
                and distance < best_distance
            ):
                best = (candidate, target)
                best_distance = distance
        return (start, None) if best is None else best

    x, vertical_guide = snap_axis("x", rect.x, rect.width, viewport_width)
    y, horizontal_guide = snap_axis("y", rect.y, rect.height, viewport_height)
    guides = tuple(
        (axis, position)
        for axis, position in (("x", vertical_guide), ("y", horizontal_guide))
        if position is not None
    )
    return NormalizedRect(x=x, y=y, width=rect.width, height=rect.height), guides


def resize_rect(
    original: NormalizedRect,
    handle: str,
    dx: float,
    dy: float,
    *,
    preserve_aspect: bool,
) -> NormalizedRect:
    if handle not in HANDLE_NAMES:
        raise ValueError(f"Unknown resize handle: {handle}")
    moves_left = "left" in handle
    moves_right = "right" in handle
    moves_top = "top" in handle
    moves_bottom = "bottom" in handle
    if not preserve_aspect:
        original_right = original.x + original.width
        original_bottom = original.y + original.height
        left = (
            min(original_right - MINIMUM_LAYER_SIZE, max(0.0, original.x + dx))
            if moves_left
            else original.x
        )
        right = (
            min(1.0, max(original.x + MINIMUM_LAYER_SIZE, original_right + dx))
            if moves_right
            else original_right
        )
        top = (
            min(original_bottom - MINIMUM_LAYER_SIZE, max(0.0, original.y + dy))
            if moves_top
            else original.y
        )
        bottom = (
            min(1.0, max(original.y + MINIMUM_LAYER_SIZE, original_bottom + dy))
            if moves_bottom
            else original_bottom
        )
        return NormalizedRect(x=left, y=top, width=right - left, height=bottom - top)

    horizontal_scale = (
        (original.width - dx) / original.width
        if moves_left
        else (original.width + dx) / original.width
        if moves_right
        else 1.0
    )
    vertical_scale = (
        (original.height - dy) / original.height
        if moves_top
        else (original.height + dy) / original.height
        if moves_bottom
        else 1.0
    )
    if (moves_left or moves_right) and (moves_top or moves_bottom):
        scale = (
            horizontal_scale
            if abs(horizontal_scale - 1.0) >= abs(vertical_scale - 1.0)
            else vertical_scale
        )
    elif moves_left or moves_right:
        scale = horizontal_scale
    else:
        scale = vertical_scale
    center_x = original.x + original.width / 2.0
    center_y = original.y + original.height / 2.0
    maximum_width = (
        original.x + original.width
        if moves_left
        else 1.0 - original.x
        if moves_right
        else 2.0 * min(center_x, 1.0 - center_x)
    )
    maximum_height = (
        original.y + original.height
        if moves_top
        else 1.0 - original.y
        if moves_bottom
        else 2.0 * min(center_y, 1.0 - center_y)
    )
    minimum_scale = max(
        MINIMUM_LAYER_SIZE / original.width,
        MINIMUM_LAYER_SIZE / original.height,
    )
    maximum_scale = min(
        maximum_width / original.width,
        maximum_height / original.height,
    )
    scale = min(maximum_scale, max(minimum_scale, scale))
    width = original.width * scale
    height = original.height * scale
    x = (
        original.x + original.width - width
        if moves_left
        else original.x
        if moves_right
        else center_x - width / 2.0
    )
    y = (
        original.y + original.height - height
        if moves_top
        else original.y
        if moves_bottom
        else center_y - height / 2.0
    )
    return NormalizedRect(x=x, y=y, width=width, height=height)


def crop_geometry(
    original_rect: NormalizedRect,
    original_crop: Crop,
    handle: str,
    dx: float,
    dy: float,
) -> tuple[NormalizedRect, Crop]:
    if handle not in HANDLE_NAMES:
        raise ValueError(f"Unknown crop handle: {handle}")
    left = original_rect.x
    top = original_rect.y
    right = original_rect.x + original_rect.width
    bottom = original_rect.y + original_rect.height
    crop_left = original_crop.left
    crop_top = original_crop.top
    crop_right = original_crop.right
    crop_bottom = original_crop.bottom
    visible_x = 1.0 - crop_left - crop_right
    visible_y = 1.0 - crop_top - crop_bottom
    if "left" in handle:
        edge_delta = min(original_rect.width - MINIMUM_LAYER_SIZE, max(-left, dx))
        candidate = crop_left + edge_delta / original_rect.width * visible_x
        crop_left = min(1.0 - crop_right - MINIMUM_VISIBLE_SOURCE, max(0.0, candidate))
        left += (crop_left - original_crop.left) * original_rect.width / visible_x
    if "right" in handle:
        edge_delta = min(1.0 - right, max(-(original_rect.width - MINIMUM_LAYER_SIZE), dx))
        candidate = crop_right - edge_delta / original_rect.width * visible_x
        crop_right = min(1.0 - crop_left - MINIMUM_VISIBLE_SOURCE, max(0.0, candidate))
        right -= (crop_right - original_crop.right) * original_rect.width / visible_x
    if "top" in handle:
        edge_delta = min(original_rect.height - MINIMUM_LAYER_SIZE, max(-top, dy))
        candidate = crop_top + edge_delta / original_rect.height * visible_y
        crop_top = min(1.0 - crop_bottom - MINIMUM_VISIBLE_SOURCE, max(0.0, candidate))
        top += (crop_top - original_crop.top) * original_rect.height / visible_y
    if "bottom" in handle:
        edge_delta = min(1.0 - bottom, max(-(original_rect.height - MINIMUM_LAYER_SIZE), dy))
        candidate = crop_bottom - edge_delta / original_rect.height * visible_y
        crop_bottom = min(1.0 - crop_top - MINIMUM_VISIBLE_SOURCE, max(0.0, candidate))
        bottom -= (crop_bottom - original_crop.bottom) * original_rect.height / visible_y
    return (
        NormalizedRect(x=left, y=top, width=right - left, height=bottom - top),
        Crop(left=crop_left, top=crop_top, right=crop_right, bottom=crop_bottom),
    )


__all__ = [
    "FramingRect",
    "HANDLE_NAMES",
    "NORMALIZED_CANVAS_ASPECT",
    "crop_for_framing_rect",
    "crop_geometry",
    "expand_focus_rect_to_aspect",
    "fill_crop_to_canvas",
    "inscribed_aspect_rect",
    "intersect_rect",
    "move_framing_rect",
    "recover_uncropped_rect",
    "resize_framing_rect",
    "resize_rect",
    "scale_framing_rect",
    "snap_move_rect",
    "source_framing_geometry",
]
