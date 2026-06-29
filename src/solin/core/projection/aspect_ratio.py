from __future__ import annotations

from dataclasses import dataclass
from math import gcd
from typing import Any


__all__ = (
    "DEFAULT_PROJECTION_ASPECT_RATIO",
    "ProjectionAspectRatio",
    "projection_aspect_ratio_from_windows",
)


@dataclass(frozen=True, slots=True)
class ProjectionAspectRatio:
    """Aspect ratio resolved from a projection target or a safe fallback."""

    width: int
    height: int
    source: str = "projection"

    @classmethod
    def from_size(
        cls,
        width: int,
        height: int,
        *,
        source: str = "projection",
    ) -> "ProjectionAspectRatio | None":
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return None
        return cls(width=width, height=height, source=source)

    @property
    def value(self) -> float:
        return self.width / self.height

    @property
    def is_fallback(self) -> bool:
        return self.source == "fallback"

    @property
    def label(self) -> str:
        divisor = gcd(self.width, self.height)
        width = self.width // divisor
        height = self.height // divisor
        if (width, height) == (8, 5):
            return "16:10"
        return f"{width}:{height}"


DEFAULT_PROJECTION_ASPECT_RATIO = ProjectionAspectRatio(
    width=16,
    height=9,
    source="fallback",
)


def projection_aspect_ratio_from_windows(
    windows: list[Any] | tuple[Any, ...],
) -> ProjectionAspectRatio:
    """Return the first visible media projection window ratio, or 16:9."""

    for window in windows:
        if not _is_visible(window):
            continue
        ratio = _aspect_ratio_from_window_screen(window)
        if ratio is not None:
            return ratio
        ratio = _aspect_ratio_from_window_geometry(window)
        if ratio is not None:
            return ratio
    return DEFAULT_PROJECTION_ASPECT_RATIO


def _is_visible(window: Any) -> bool:
    try:
        if not bool(window.isVisible()):
            return False
    except Exception:  # noqa: BLE001 - defensive Qt object lifetime boundary
        return False

    try:
        return not bool(window.isMinimized())
    except Exception:  # noqa: BLE001 - defensive optional QWidget API boundary
        return True


def _aspect_ratio_from_window_screen(window: Any) -> ProjectionAspectRatio | None:
    try:
        screen = window.screen()
    except Exception:  # noqa: BLE001 - defensive Qt object lifetime boundary
        screen = None
    if screen is None:
        return None

    try:
        geometry = screen.geometry()
    except Exception:  # noqa: BLE001 - defensive QScreen capability boundary
        return None
    return _aspect_ratio_from_geometry(geometry)


def _aspect_ratio_from_window_geometry(window: Any) -> ProjectionAspectRatio | None:
    try:
        geometry = window.geometry()
    except Exception:  # noqa: BLE001 - defensive QWidget capability boundary
        return None
    return _aspect_ratio_from_geometry(geometry)


def _aspect_ratio_from_geometry(geometry: Any) -> ProjectionAspectRatio | None:
    width = _dimension(geometry, "width")
    height = _dimension(geometry, "height")
    if width is None or height is None:
        return None
    return ProjectionAspectRatio.from_size(width, height)


def _dimension(geometry: Any, name: str) -> int | None:
    try:
        value = getattr(geometry, name)
    except Exception:  # noqa: BLE001 - defensive object capability boundary
        return None
    try:
        raw_value = value() if callable(value) else value
    except Exception:  # noqa: BLE001 - defensive geometry accessor boundary
        return None
    if not isinstance(raw_value, (int, float, str, bytes, bytearray)):
        return None
    try:
        return int(raw_value)
    except Exception:  # noqa: BLE001 - defensive object capability boundary
        return None
