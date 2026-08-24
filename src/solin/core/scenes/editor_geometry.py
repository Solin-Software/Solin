"""Pure editor geometry independent from QWidget, QML, and the renderer."""

from __future__ import annotations

from solin.core.scenes.model import Crop, NormalizedRect


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


__all__ = ["HANDLE_NAMES", "crop_geometry", "resize_rect", "snap_move_rect"]
