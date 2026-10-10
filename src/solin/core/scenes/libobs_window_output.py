"""Bind libobs ``Display``s to the app's native projection-window handles.

``set_window_targets`` hands the engine the application's native window handles;
each becomes an ``obs_display`` that renders the composited main texture
(letterboxed). libobs runs in the sidecar while the windows belong to the app
process — ``pylibobs`` ``Display.from_window`` binds to the server-global window
handle (X11 ``Window`` XID / Win32 ``HWND``), the same cross-process pattern the
native engine uses; on X11 it opens its own connection to ``$DISPLAY``.

Each target is routed to exactly one thing, in precedence order: a ``scene_id``
renders that one scene directly on the GPU (the editor's preview surface); a
target on the projection bus renders the projection transition
(:class:`~solin.core.scenes.libobs_projection_route.LibobsProjectionRoute`), which
is independent of the program; anything else renders the composited main texture
(the program that also feeds the virtual camera and the recording).

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

# What a window shows. Precedence when routing a target: an explicit scene wins
# (the editor's direct-GPU surface), then the projection output, else the program.
_ROUTE_PROGRAM = "program"
_ROUTE_PROJECTION = "projection"
_ROUTE_SCENE = "scene"
_PROJECTION_BUS = "media_windows"


def _route_for(target: dict, scene_id: str) -> str:
    if scene_id:
        return _ROUTE_SCENE
    if str(target.get("bus_id", "") or "") == _PROJECTION_BUS:
        return _ROUTE_PROJECTION
    return _ROUTE_PROGRAM

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
        self._scene_ids: dict[int, str] = {}  # handle → scene to render ("" = none)
        # handle → which output this window shows. Replaced wholesale (an atomic
        # attribute store) so the draw callback can read it without a lock.
        self._routes: dict[int, str] = {}
        self._scene_resolver: Callable[[str], Any] | None = None
        self._projection_resolver: Callable[[], Any] | None = None
        self._lock = threading.RLock()

    @property
    def handles(self) -> tuple[int, ...]:
        return tuple(self._displays)

    @property
    def render_targets(self) -> tuple[tuple[str, str], ...]:
        """Live display routes, read without blocking the graphics thread."""
        routes = self._routes
        scenes = self._scene_ids
        return tuple((route, scenes.get(handle, "")) for handle, route in routes.items())

    @property
    def hydrate_lock(self) -> "threading.RLock":
        """Held by per-scene draw callbacks; the sidecar holds it across a graph
        rebuild so a callback can't resolve a scene mid-clear."""
        return self._lock

    def set_projection_resolver(self, resolver: Callable[[], Any] | None) -> None:
        """Install the callable returning the projection transition's raw pointer."""
        self._projection_resolver = resolver

    def set_scene_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        """Install the ``scene_id -> source`` resolver (the live scene graph's
        ``scene_source``). Re-resolved every frame, so a rebuilt graph is picked up."""
        with self._lock:
            self._scene_resolver = resolver

    def set_targets(self, targets: list) -> None:
        """Reconcile the live displays against the requested window targets."""
        canvas = self._runtime.video
        wanted: dict[int, tuple[int, int, str, str]] = {}
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
            wanted[handle] = (width, height, scene_id, _route_for(target, scene_id))

        with self._lock:
            for handle in list(self._displays):
                if handle not in wanted:
                    self._release_one(handle)

            routes = dict(self._routes)
            for handle, (width, height, scene_id, route) in wanted.items():
                self._scene_ids[handle] = scene_id  # picked up live by the draw callback
                routes[handle] = route
                display = self._displays.get(handle)
                if display is None:
                    self._create(handle, width, height, canvas)
                elif self._sizes.get(handle) != (width, height):
                    try:
                        display.resize(width, height)
                        self._sizes[handle] = (width, height)
                    except Exception:  # noqa: BLE001 - libobs boundary
                        log.warning("libobs display resize failed for handle %d", handle, exc_info=True)
            # One store, so a draw callback never sees a half-updated map.
            self._routes = {
                handle: routes[handle] for handle in wanted if handle in self._displays
            }

    def _create(self, handle: int, width: int, height: int, canvas: Any) -> None:
        try:
            display = self._display_factory(handle, width, height, _BACKGROUND_COLOR)
        except Exception:  # noqa: BLE001 - a bad handle must not crash the engine
            log.warning("Could not create a libobs display for window handle %d", handle, exc_info=True)
            return
        canvas_w, canvas_h = canvas.width, canvas.height

        def _draw(display_cx: int, display_cy: int) -> None:
            # This runs on libobs' graphics thread while it holds the graphics
            # mutex for the whole render pass. The reconcile/hydrate thread holds
            # ``self._lock`` while creating/destroying a display or rebuilding the
            # graph — operations that in turn need that same graphics mutex. So we
            # must NEVER block on ``self._lock`` here: doing so is a lock-order
            # inversion (self._lock ↔ graphics mutex) that deadlocks the whole
            # engine the instant an edit changes a window handle or re-hydrates.
            from pylibobs.display import (
                render_main_texture_letterboxed,
                render_source_letterboxed,
            )

            route = self._routes.get(handle, _ROUTE_PROGRAM)
            if route == _ROUTE_PROGRAM:
                # The main texture is owned by libobs and always valid: no lock.
                render_main_texture_letterboxed(canvas_w, canvas_h, display_cx, display_cy)
                return
            if route == _ROUTE_PROJECTION:
                # The projection transition is created once and released only at
                # shutdown, so its pointer can be read without the lock too.
                resolver = self._projection_resolver
                pointer = resolver() if resolver is not None else None
                render_source_letterboxed(
                    pointer, canvas_w, canvas_h, display_cx, display_cy)
                return
            # A per-scene route renders a *borrowed* scene source that a concurrent
            # rebuild may release, so it needs the lock — non-blockingly. When the
            # lock is busy we render NOTHING rather than falling back to the main
            # texture: that fallback would flash the program (camera and all) onto
            # a screen that is deliberately showing something else.
            if self._lock.acquire(blocking=False):
                try:
                    scene_id = self._scene_ids.get(handle, "")
                    resolver = self._scene_resolver
                    if scene_id and resolver is not None:
                        source = resolver(scene_id)
                        render_source_letterboxed(
                            getattr(source, "_ptr", None) if source is not None else None,
                            canvas_w, canvas_h, display_cx, display_cy)
                finally:
                    self._lock.release()

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
        self._routes = {k: v for k, v in self._routes.items() if k != handle}
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
