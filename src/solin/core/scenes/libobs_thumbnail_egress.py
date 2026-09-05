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

Sources that are not showing anywhere render as they last were (or black): giving
every thumbnailed scene show refs would start every camera in every scene, which
is exactly what we do not want to do to a meeting.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA

log = logging.getLogger(__name__)

# Thumbnails refresh slowly on purpose: the program render comes first.
_RENDER_INTERVAL_S = 1.0 / 8.0


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
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="solin-thumbnail-render", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        import pylibobs as ob

        canvas = self._runtime.video
        while not self._stop.wait(_RENDER_INTERVAL_S):
            with self._lock:
                writer = self._writer
                scene_ids = self._scene_ids
                cell_width, cell_height = self._cell
                if writer is None or not scene_ids or cell_width <= 0:
                    continue
                row_bytes = cell_width * 4
                atlas = bytearray(row_bytes * cell_height * len(scene_ids))
                for index, scene_id in enumerate(scene_ids):
                    source = self._scene_resolver(scene_id)
                    if source is None:
                        continue
                    try:
                        result = ob.render_source_to_bgra(
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
