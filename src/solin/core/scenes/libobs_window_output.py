"""Bind libobs ``Display``s to the app's native projection-window handles.

``set_window_targets`` hands the engine the application's native window handles;
each becomes an ``obs_display`` that renders the composited main texture
(letterboxed). libobs runs in the sidecar while the windows belong to the app
process — ``pylibobs`` ``Display.from_window`` binds to the server-global window
handle (X11 ``Window`` XID / Win32 ``HWND``), the same cross-process pattern the
native engine uses; on X11 it opens its own connection to ``$DISPLAY``.

The rendered content is the main texture (the composited output channels), so the
program scene routed by :class:`~solin.core.scenes.libobs_scene_builder.LibobsSceneGraph`
appears on every target window. Per-bus views are a later refinement.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

# Opaque black for the letterbox bars outside the rendered canvas.
_BACKGROUND_COLOR = 0xFF000000

# A display factory yields a Display-like object exposing add_draw_callback /
# resize / release. Injected so the manager is testable without pylibobs.
DisplayFactory = Callable[[int, int, int, int], Any]


def _default_display_factory(handle: int, width: int, height: int, background: int) -> Any:
    from pylibobs.display import Display

    return Display.from_window(handle, width, height, background_color=background)


class LibobsWindowOutput:
    """Manages one ``obs_display`` per native window handle."""

    def __init__(self, runtime: Any, display_factory: DisplayFactory | None = None) -> None:
        self._runtime = runtime
        self._display_factory = display_factory or _default_display_factory
        self._displays: dict[int, Any] = {}
        self._sizes: dict[int, tuple[int, int]] = {}

    @property
    def handles(self) -> tuple[int, ...]:
        return tuple(self._displays)

    def set_targets(self, targets: list) -> None:
        """Reconcile the live displays against the requested window targets."""
        canvas = self._runtime.video
        wanted: dict[int, tuple[int, int]] = {}
        for target in targets:
            if not isinstance(target, dict) or not target.get("visible", True):
                continue
            handle = int(target.get("native_handle") or 0)
            if not handle:
                continue
            dpr = float(target.get("device_pixel_ratio", 1.0) or 1.0)
            width = max(1, round(int(target.get("width", 1)) * dpr))
            height = max(1, round(int(target.get("height", 1)) * dpr))
            wanted[handle] = (width, height)

        for handle in list(self._displays):
            if handle not in wanted:
                self._release_one(handle)

        for handle, (width, height) in wanted.items():
            display = self._displays.get(handle)
            if display is None:
                self._create(handle, width, height, canvas)
            elif self._sizes.get(handle) != (width, height):
                try:
                    display.resize(width, height)
                    self._sizes[handle] = (width, height)
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.warning("libobs display resize failed for handle %d", handle, exc_info=True)

    def _create(self, handle: int, width: int, height: int, canvas: Any) -> None:
        try:
            display = self._display_factory(handle, width, height, _BACKGROUND_COLOR)
        except Exception:  # noqa: BLE001 - a bad handle must not crash the engine
            log.warning("Could not create a libobs display for window handle %d", handle, exc_info=True)
            return
        canvas_w, canvas_h = canvas.width, canvas.height

        def _draw(display_cx: int, display_cy: int) -> None:
            from pylibobs.display import render_main_texture_letterboxed

            render_main_texture_letterboxed(canvas_w, canvas_h, display_cx, display_cy)

        try:
            display.add_draw_callback(_draw)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not add a draw callback for window handle %d", handle, exc_info=True)
            try:
                display.release()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.debug("display release after failed callback errored", exc_info=True)
            return
        self._displays[handle] = display
        self._sizes[handle] = (width, height)

    def _release_one(self, handle: int) -> None:
        display = self._displays.pop(handle, None)
        self._sizes.pop(handle, None)
        if display is None:
            return
        try:
            display.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("display release errored for handle %d", handle, exc_info=True)

    def shutdown(self) -> None:
        for handle in list(self._displays):
            self._release_one(handle)
