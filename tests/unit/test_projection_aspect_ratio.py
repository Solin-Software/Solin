from __future__ import annotations

from solin.core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
    projection_aspect_ratio_from_windows,
)


class _Geometry:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _Screen:
    def __init__(self, width: int, height: int) -> None:
        self._geometry = _Geometry(width, height)

    def geometry(self) -> _Geometry:
        return self._geometry


class _Window:
    def __init__(
        self,
        *,
        visible: bool = True,
        minimized: bool = False,
        screen: _Screen | None = None,
        geometry: _Geometry | None = None,
    ) -> None:
        self._visible = visible
        self._minimized = minimized
        self._screen = screen
        self._geometry = geometry or _Geometry(0, 0)

    def isVisible(self) -> bool:
        return self._visible

    def isMinimized(self) -> bool:
        return self._minimized

    def screen(self) -> _Screen | None:
        return self._screen

    def geometry(self) -> _Geometry:
        return self._geometry


def test_projection_aspect_ratio_formats_known_display_label() -> None:
    ratio = ProjectionAspectRatio.from_size(2560, 1600)

    assert ratio is not None
    assert ratio.label == "16:10"
    assert ratio.value == 1.6


def test_projection_aspect_ratio_uses_first_visible_projection_screen() -> None:
    hidden = _Window(visible=False, screen=_Screen(1024, 768))
    visible = _Window(screen=_Screen(1920, 1200))

    ratio = projection_aspect_ratio_from_windows([hidden, visible])

    assert ratio.label == "16:10"
    assert not ratio.is_fallback


def test_projection_aspect_ratio_skips_minimized_projection_windows() -> None:
    minimized = _Window(minimized=True, screen=_Screen(1920, 1080))

    ratio = projection_aspect_ratio_from_windows([minimized])

    assert ratio == DEFAULT_PROJECTION_ASPECT_RATIO


def test_projection_aspect_ratio_falls_back_to_window_geometry() -> None:
    window = _Window(screen=None, geometry=_Geometry(1024, 768))

    ratio = projection_aspect_ratio_from_windows([window])

    assert ratio.label == "4:3"


def test_projection_aspect_ratio_falls_back_without_visible_projection() -> None:
    ratio = projection_aspect_ratio_from_windows([])

    assert ratio == DEFAULT_PROJECTION_ASPECT_RATIO
