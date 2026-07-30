from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LayerGeometry:
    id: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class SnapGuide:
    axis: str
    position: float
    id: str


@dataclass(frozen=True, slots=True)
class SnapResult:
    x: float
    y: float
    snap_x: str = ""
    snap_y: str = ""
    guides: tuple[SnapGuide, ...] = ()


_VERTICAL_TARGETS = (
    (0.05, "safe-left", "start"),
    (1 / 3, "third-left", "center"),
    (0.5, "center", "center"),
    (2 / 3, "third-right", "center"),
    (0.95, "safe-right", "end"),
)
_HORIZONTAL_TARGETS = (
    (0.05, "safe-top", "start"),
    (1 / 3, "third-top", "center"),
    (0.5, "middle", "center"),
    (2 / 3, "third-bottom", "center"),
    (0.95, "safe-bottom", "end"),
)


def snap_layer(
    moving: LayerGeometry,
    *,
    viewport_width: float,
    viewport_height: float,
    others: Iterable[LayerGeometry] = (),
    active_x: str = "",
    active_y: str = "",
    disabled: bool = False,
    lock_axis: str = "",
    origin_x: float | None = None,
    origin_y: float | None = None,
    engage_px: float = 8.0,
    release_px: float = 12.0,
) -> SnapResult:
    """Snap a normalized layer rectangle to semantic guides and peer edges."""

    if disabled or viewport_width <= 0 or viewport_height <= 0:
        return SnapResult(moving.x, moving.y)

    x = moving.x
    y = moving.y
    if lock_axis == "x" and origin_y is not None:
        y = origin_y
    elif lock_axis == "y" and origin_x is not None:
        x = origin_x

    x_targets = list(_VERTICAL_TARGETS)
    y_targets = list(_HORIZONTAL_TARGETS)
    for other in others:
        if other.id == moving.id:
            continue
        x_targets.extend(
            (
                (other.x, f"{other.id}:left", "any"),
                (other.x + other.width / 2, f"{other.id}:center", "any"),
                (other.x + other.width, f"{other.id}:right", "any"),
            )
        )
        y_targets.extend(
            (
                (other.y, f"{other.id}:top", "any"),
                (other.y + other.height / 2, f"{other.id}:middle", "any"),
                (other.y + other.height, f"{other.id}:bottom", "any"),
            )
        )

    x_match = _best_axis_match(
        start=x,
        size=moving.width,
        viewport=viewport_width,
        targets=x_targets,
        active=active_x,
        engage_px=engage_px,
        release_px=release_px,
    )
    y_match = _best_axis_match(
        start=y,
        size=moving.height,
        viewport=viewport_height,
        targets=y_targets,
        active=active_y,
        engage_px=engage_px,
        release_px=release_px,
    )

    guides: list[SnapGuide] = []
    snap_x = ""
    snap_y = ""
    if x_match is not None:
        x, position, snap_x = x_match
        guides.append(SnapGuide("x", position, snap_x))
    if y_match is not None:
        y, position, snap_y = y_match
        guides.append(SnapGuide("y", position, snap_y))

    x = max(-moving.width + 0.02, min(0.98, x))
    y = max(-moving.height + 0.02, min(0.98, y))
    return SnapResult(x=x, y=y, snap_x=snap_x, snap_y=snap_y, guides=tuple(guides))


def benchmark_snap(iterations: int = 10_000) -> dict[str, float]:
    moving = LayerGeometry("title", 0.491, 0.42, 0.4, 0.15)
    others = (
        LayerGeometry("label", 0.2, 0.1, 0.6, 0.05),
        LayerGeometry("speaker", 0.3, 0.75, 0.4, 0.05),
        LayerGeometry("congregation", 0.3, 0.82, 0.4, 0.04),
    )
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        started = perf_counter()
        snap_layer(
            moving,
            viewport_width=1200,
            viewport_height=675,
            others=others,
        )
        samples.append((perf_counter() - started) * 1000)
    samples.sort()
    return {
        "p50_ms": samples[len(samples) // 2],
        "p95_ms": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
    }


def _best_axis_match(
    *,
    start: float,
    size: float,
    viewport: float,
    targets: list[tuple[float, str, str]],
    active: str,
    engage_px: float,
    release_px: float,
) -> tuple[float, float, str] | None:
    best: tuple[float, float, str] | None = None
    best_distance = float("inf")
    all_points = ((start, 0.0), (start + size / 2, size / 2), (start + size, size))
    for target, target_id, anchor in targets:
        threshold = release_px if target_id == active else engage_px
        if anchor == "start":
            points = all_points[:1]
        elif anchor == "center":
            points = all_points[1:2]
        elif anchor == "end":
            points = all_points[2:]
        else:
            points = all_points
        for point, offset in points:
            distance = abs(point - target) * viewport
            if distance <= threshold and distance < best_distance:
                best_distance = distance
                best = (target - offset, target, target_id)
    return best
