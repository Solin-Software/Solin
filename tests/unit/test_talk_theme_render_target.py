from __future__ import annotations

from solin.core.talk_theme.render_target import resolve_theme_render_target


class _Geometry:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _Screen:
    def __init__(self, width: int, height: int, dpr: float = 1.0) -> None:
        self._geometry = _Geometry(width, height)
        self._dpr = dpr

    def geometry(self) -> _Geometry:
        return self._geometry

    def devicePixelRatio(self) -> float:
        return self._dpr


class _Window:
    def __init__(self, screen: _Screen, *, visible: bool = True) -> None:
        self._screen = screen
        self._visible = visible

    def isVisible(self) -> bool:
        return self._visible

    def isMinimized(self) -> bool:
        return False

    def screen(self) -> _Screen:
        return self._screen


def test_render_target_uses_native_output_pixels() -> None:
    target = resolve_theme_render_target(
        [_Window(_Screen(1920, 1080, 1.5))],
        follow_output=True,
    )

    assert (target.width, target.height) == (2880, 1620)
    assert target.source == "projection"


def test_render_target_caps_ultra_high_resolution_proportionally() -> None:
    target = resolve_theme_render_target(
        [_Window(_Screen(5120, 2880))],
        follow_output=True,
    )

    assert (target.width, target.height) == (3840, 2160)


def test_render_target_uses_1080p_when_auto_is_disabled_or_unavailable() -> None:
    fixed = resolve_theme_render_target([], follow_output=False)
    fallback = resolve_theme_render_target([], follow_output=True)

    assert (fixed.width, fixed.height, fixed.source) == (1920, 1080, "fixed")
    assert (fallback.width, fallback.height, fallback.source) == (1920, 1080, "fallback")
