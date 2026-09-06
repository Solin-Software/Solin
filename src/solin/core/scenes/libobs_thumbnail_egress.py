"""Live scene thumbnails for the scenes panel (libobs -> app, one shared block).

Each scene card in the panel shows a small live render of its scene. Rather than
one shared-memory channel per card, every thumbnail is rendered into one row of a
single vertical atlas: the app creates a block ``cell_width x (cell_height * N)``
and the sidecar writes scene ``i`` into row ``i`` of the scene-id list it was
given, so the app can slice rows without any per-card plumbing.

Rendering runs on a worker thread that enters the graphics context per frame
(serialised with the main render loop by libobs' graphics mutex), at a deliberately
low rate — these are thumbnails, and the program's own render must stay first in
line. Scene sources are **borrowed** from the scene graph, so a render and a graph
rebuild are mutually excluded by ``_lock``.

Thumbnailed scenes are show-ref'd so their cameras and media actually run and every
card is live, not a frozen last frame. Show refs are not activate refs, so none of
this reaches the program audio mix. Cameras are shared per device by the runtime,
so several scenes using one camera still open the device once.

The refs are the delicate part: ``scene_source`` hands back a BORROWED source, so a
graph rebuild destroys it. Every ref is therefore dropped before a rebuild and
retaken afterwards (:meth:`suspend` / :meth:`resume`, called by the sidecar around
``graph.hydrate``) — decrementing a show ref on a freed source would crash the
engine mid-meeting.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA

log = logging.getLogger(__name__)

# Fast enough to read as live, still well under the program's own render.
_RENDER_INTERVAL_S = 1.0 / 20.0


class LibobsThumbnailEgress:
    """Renders one small frame per scene into a shared vertical atlas."""

    def __init__(self, runtime: Any, scene_resolver: Callable[[str], Any]) -> None:
        self._runtime = runtime
        self._scene_resolver = scene_resolver
        self._lock = threading.Lock()
        self._writer: Any = None
        self._handle_token = ""
        self._scene_ids: tuple[str, ...] = ()
        self._cell = (0, 0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._suspended = False
        self._show_refs: list[Any] = []  # raw pointers we have inc_showing'd

    @property
    def scene_ids(self) -> tuple[str, ...]:
        return self._scene_ids

    def configure(
        self,
        descriptor: object,
        scene_ids: tuple[str, ...],
        cell_width: int,
        cell_height: int,
    ) -> None:
        """Attach to the app's atlas block, or detach when it goes away."""
        valid = (
            isinstance(descriptor, dict)
            and descriptor.get("transport") == SHARED_MEMORY_BGRA
            and scene_ids
            and cell_width > 0
            and cell_height > 0
        )
        if not valid:
            self._detach()
            return
        token = str(descriptor.get("handle_token") or "")
        width = int(descriptor.get("width") or 0)
        height = int(descriptor.get("height") or 0)
        if not token or width <= 0 or height <= 0:
            self._detach()
            return
        with self._lock:
            same = (
                self._writer is not None
                and self._handle_token == token
                and self._scene_ids == tuple(scene_ids)
                and self._cell == (cell_width, cell_height)
            )
        if same:
            return
        self._detach()
        from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter

        try:
            writer = SharedFrameChannelWriter(width, height, name=token, create=False)
        except Exception:  # noqa: BLE001 - the app may not have created the block yet
            log.warning("could not attach the thumbnail writer to %r", token, exc_info=True)
            return
        with self._lock:
            self._writer = writer
            self._handle_token = token
            self._scene_ids = tuple(scene_ids)
            self._cell = (cell_width, cell_height)
            if not self._suspended:
                self._acquire_show_refs()
        self._ensure_thread()

    # ── show refs ──────────────────────────────────────────────────────────

    def _acquire_show_refs(self) -> None:
        """Show-ref every thumbnailed scene so its sources actually run."""
        if self._show_refs:
            return
        try:
            from pylibobs._ffi import get_lib

            lib = get_lib()
        except Exception:  # noqa: BLE001 - unwrapped libobs symbols
            log.warning("could not reach obs_source_inc_showing", exc_info=True)
            return
        for scene_id in self._scene_ids:
            source = self._scene_resolver(scene_id)
            pointer = getattr(source, "_ptr", None) if source is not None else None
            if pointer is None:
                continue
            try:
                lib.obs_source_inc_showing(pointer)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("thumbnail show-ref failed for %r", scene_id, exc_info=True)
                continue
            self._show_refs.append(pointer)

    def _release_show_refs(self) -> None:
        pointers, self._show_refs = self._show_refs, []
        if not pointers:
            return
        try:
            from pylibobs._ffi import get_lib

            lib = get_lib()
        except Exception:  # noqa: BLE001 - unwrapped libobs symbols
            return
        for pointer in pointers:
            try:
                lib.obs_source_dec_showing(pointer)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("thumbnail show-unref failed", exc_info=True)

    def suspend(self) -> None:
        """Stop rendering and drop show refs before the scene graph is rebuilt.

        The sources are borrowed, so they cease to exist across the rebuild; a ref
        held over it would be decremented against freed memory.
        """
        with self._lock:
            self._suspended = True
            self._release_show_refs()

    def resume(self) -> None:
        """Re-resolve the rebuilt scenes and take fresh show refs."""
        with self._lock:
            self._suspended = False
            if self._writer is not None:
                self._acquire_show_refs()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="solin-thumbnail-render", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        from solin.core.media.obs_source_render import resolve_render_source_to_bgra

        render_to_bgra = resolve_render_source_to_bgra()

        canvas = self._runtime.video
        while not self._stop.wait(_RENDER_INTERVAL_S):
            with self._lock:
                writer = self._writer
                scene_ids = self._scene_ids
                cell_width, cell_height = self._cell
                if self._suspended or writer is None or not scene_ids or cell_width <= 0:
                    continue
                row_bytes = cell_width * 4
                atlas = bytearray(row_bytes * cell_height * len(scene_ids))
                for index, scene_id in enumerate(scene_ids):
                    source = self._scene_resolver(scene_id)
                    if source is None:
                        continue
                    try:
                        result = render_to_bgra(
                            source, cell_width, cell_height,
                            canvas_width=canvas.width, canvas_height=canvas.height,
                        )
                    except Exception:  # noqa: BLE001 - libobs graphics boundary
                        log.debug("thumbnail render errored for %r", scene_id, exc_info=True)
                        continue
                    if result is None:
                        continue
                    data, stride = result
                    base = index * cell_height * row_bytes
                    for row in range(cell_height):
                        src = row * stride
                        dst = base + row * row_bytes
                        atlas[dst : dst + row_bytes] = data[src : src + row_bytes]
                try:
                    writer.write(bytes(atlas), stride=row_bytes)
                except Exception:  # noqa: BLE001 - shared-memory boundary
                    log.debug("thumbnail atlas write errored", exc_info=True)

    def _detach(self) -> None:
        with self._lock:
            self._release_show_refs()
            writer, self._writer = self._writer, None
            self._handle_token = ""
            self._scene_ids = ()
            self._cell = (0, 0)
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("thumbnail writer close errored", exc_info=True)

    def shutdown(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._detach()
