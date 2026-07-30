from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


FALLBACK_RENDER_WIDTH = 1920
FALLBACK_RENDER_HEIGHT = 1080
MAX_RENDER_WIDTH = 3840
MAX_RENDER_HEIGHT = 2160


@dataclass(frozen=True, slots=True)
class ThemeRenderTarget:
    width: int
    height: int
    source: str

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    @property
    def label(self) -> str:
        return f"{self.width} × {self.height}"


def resolve_theme_render_target(
    windows: Iterable[Any],
    *,
    follow_output: bool,
) -> ThemeRenderTarget:
    if not follow_output:
        return ThemeRenderTarget(FALLBACK_RENDER_WIDTH, FALLBACK_RENDER_HEIGHT, "fixed")

    for window in windows:
        if not _is_visible(window):
            continue
        size = _native_screen_size(window) or _native_window_size(window)
        if size is None:
            continue
        width, height = _fit_within_cap(*size)
        return ThemeRenderTarget(width, height, "projection")
    return ThemeRenderTarget(FALLBACK_RENDER_WIDTH, FALLBACK_RENDER_HEIGHT, "fallback")


def _is_visible(window: Any) -> bool:
    try:
        if not bool(window.isVisible()):
            return False
        return not bool(window.isMinimized())
    except Exception:  # noqa: BLE001 - defensive Qt object boundary
        return False


def _native_screen_size(window: Any) -> tuple[int, int] | None:
    try:
        screen = window.screen()
        geometry = screen.geometry()
        dpr = float(screen.devicePixelRatio())
    except Exception:  # noqa: BLE001 - defensive QScreen boundary
        return None
    return _scaled_size(geometry, dpr)


def _native_window_size(window: Any) -> tuple[int, int] | None:
    try:
        geometry = window.geometry()
        dpr = float(window.devicePixelRatioF())
    except Exception:  # noqa: BLE001 - defensive QWidget boundary
        return None
    return _scaled_size(geometry, dpr)


def _scaled_size(geometry: Any, dpr: float) -> tuple[int, int] | None:
    try:
        width = round(float(geometry.width()) * max(1.0, dpr))
        height = round(float(geometry.height()) * max(1.0, dpr))
    except Exception:  # noqa: BLE001 - defensive geometry boundary
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _fit_within_cap(width: int, height: int) -> tuple[int, int]:
    scale = min(1.0, MAX_RENDER_WIDTH / width, MAX_RENDER_HEIGHT / height)
    return max(1, round(width * scale)), max(1, round(height * scale))
