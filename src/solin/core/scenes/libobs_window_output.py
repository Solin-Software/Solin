"""Bind libobs ``Display``s to the app's native projection-window handles.

``set_window_targets`` hands the engine the application's native window handles;
each becomes an ``obs_display`` that renders the composited main texture
(letterboxed). libobs runs in the sidecar while the windows belong to the app
process — ``pylibobs`` ``Display.from_window`` binds to the server-global window
handle (X11 ``Window`` XID / Win32 ``HWND``), the same cross-process pattern the
native engine uses; on X11 it opens its own connection to ``$DISPLAY``.

By default a target renders the main texture (the composited output channels), so
the program scene routed by :class:`~solin.core.scenes.libobs_scene_builder.LibobsSceneGraph`
appears on it. A target may instead carry a ``scene_id`` to render one specific
scene directly on the GPU (e.g. the editor's preview of the selected scene) — the
draw callback resolves that scene per frame through an injected resolver.

Thread-safety: draw callbacks run on libobs' graphics thread while ``hydrate``
rebuilds the scene graph on the handler thread. A per-scene callback resolves and
renders under :attr:`hydrate_lock`; the sidecar holds the same lock while it
clears/rebuilds the graph, so the callback never renders a released scene source.
"""

from __future__ import annotations

import logging
import threading
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
        self._scene_ids: dict[int, str] = {}  # handle → scene to render ("" = main mix)
        self._scene_resolver: Callable[[str], Any] | None = None
        self._lock = threading.RLock()

    @property
    def handles(self) -> tuple[int, ...]:
        return tuple(self._displays)

    @property
    def hydrate_lock(self) -> "threading.RLock":
        """Held by per-scene draw callbacks; the sidecar holds it across a graph
        rebuild so a callback can't resolve a scene mid-clear."""
        return self._lock

    def set_scene_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        """Install the ``scene_id -> source`` resolver (the live scene graph's
        ``scene_source``). Re-resolved every frame, so a rebuilt graph is picked up."""
        with self._lock:
            self._scene_resolver = resolver

    def set_targets(self, targets: list) -> None:
        """Reconcile the live displays against the requested window targets."""
        canvas = self._runtime.video
        wanted: dict[int, tuple[int, int, str]] = {}
        for target in targets:
            if not isinstance(target, dict) or not target.get("visible", True):
                continue
            handle = int(target.get("native_handle") or 0)
            if not handle:
                continue
            dpr = float(target.get("device_pixel_ratio", 1.0) or 1.0)
            width = max(1, round(int(target.get("width", 1)) * dpr))
            height = max(1, round(int(target.get("height", 1)) * dpr))
            scene_id = str(target.get("scene_id", "") or "")
            wanted[handle] = (width, height, scene_id)

        with self._lock:
            for handle in list(self._displays):
                if handle not in wanted:
                    self._release_one(handle)

            for handle, (width, height, scene_id) in wanted.items():
                self._scene_ids[handle] = scene_id  # picked up live by the draw callback
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
            # Resolve + render under the lock so a scene source can't be released
            # by a concurrent graph rebuild between resolve and render.
            with self._lock:
                scene_id = self._scene_ids.get(handle, "")
                resolver = self._scene_resolver
                if scene_id and resolver is not None:
                    from pylibobs.display import render_source_letterboxed

                    source = resolver(scene_id)
                    render_source_letterboxed(
                        getattr(source, "_ptr", None) if source is not None else None,
                        canvas_w, canvas_h, display_cx, display_cy)
                else:
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
        self._scene_ids.pop(handle, None)
        if display is None:
            return
        try:
            display.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("display release errored for handle %d", handle, exc_info=True)

    def shutdown(self) -> None:
        with self._lock:
            for handle in list(self._displays):
                self._release_one(handle)
