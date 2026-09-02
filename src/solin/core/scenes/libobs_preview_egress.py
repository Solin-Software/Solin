"""Preview egress for the scene-engine sidecar (libobs → app editor canvas).

The editor shows a live composite of the scene being edited (the MEDIA_WINDOWS
bus), which may differ from the live Program. This renders that scene off-screen
to BGRA via :func:`pylibobs.render_source_to_bgra` and writes it into the
cross-platform ``SHARED_MEMORY_BGRA`` block the app created and reads
(``SharedMemoryPreviewEgressController``). The app advertises the block's
``handle_token`` in the hydrate ``preview_egress`` descriptor; the sidecar
attaches to it as a writer.

Rendering runs on a worker thread that enters the graphics context per frame
(serialised with the main render loop by libobs' graphics mutex). The rendered
source is a **borrowed** scene source owned by the scene graph, so a render and a
scene-source swap are mutually excluded by ``_lock`` — the sidecar clears the
source before a re-hydrate releases the old scenes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA

log = logging.getLogger(__name__)

_RENDER_INTERVAL_S = 1.0 / 30.0


class LibobsPreviewEgress:
    """Renders the edited scene into the app's preview channel."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._writer: Any = None
        self._handle_token = ""
        self._width = 0
        self._height = 0
        self._scene_source: Any = None
        self._enabled = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def configure(self, descriptor: object) -> None:
        """Attach (or re-attach) the writer to the app's egress block."""
        if not isinstance(descriptor, dict) or descriptor.get("transport") != SHARED_MEMORY_BGRA:
            self._detach_writer()
            return
        token = str(descriptor.get("handle_token") or "")
        width = int(descriptor.get("width") or 0)
        height = int(descriptor.get("height") or 0)
        if not token or width <= 0 or height <= 0:
            self._detach_writer()
            return
        with self._lock:
            if (
                self._writer is not None
                and self._handle_token == token
                and self._width == width
                and self._height == height
            ):
                return  # already attached to exactly this block
        self._detach_writer()
        from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter

        try:
            writer = SharedFrameChannelWriter(width, height, name=token, create=False)
        except Exception:  # noqa: BLE001 - the app may not have created the block yet
            log.warning("could not attach preview egress writer to %r", token, exc_info=True)
            return
        with self._lock:
            self._writer = writer
            self._handle_token = token
            self._width = width
            self._height = height
        self._ensure_thread()

    def set_scene_source(self, source: Any) -> None:
        """Set the borrowed scene source to render (None to stop rendering).

        Blocks on any in-flight render so a re-hydrate can then release the old
        scene safely.
        """
        with self._lock:
            self._scene_source = source

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="solin-preview-render", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        import pylibobs as ob

        canvas = self._runtime.video
        while not self._stop.wait(_RENDER_INTERVAL_S):
            with self._lock:
                writer = self._writer
                source = self._scene_source
                enabled = self._enabled
                width = self._width
                height = self._height
                if writer is None or source is None or not enabled:
                    continue
                try:
                    result = ob.render_source_to_bgra(
                        source, width, height,
                        canvas_width=canvas.width, canvas_height=canvas.height,
                    )
                except Exception:  # noqa: BLE001 - libobs graphics boundary
                    log.warning("preview render errored", exc_info=True)
                    continue
                if result is None:
                    continue
                data, stride = result
                try:
                    writer.write(self._tight(data, stride, width, height), stride=width * 4)
                except Exception:  # noqa: BLE001 - shared-memory boundary
                    log.debug("preview egress write errored", exc_info=True)

    @staticmethod
    def _tight(data: bytes, stride: int, width: int, height: int) -> bytes:
        tight = width * 4
        if stride == tight:
            return data
        out = bytearray(tight * height)
        for row in range(height):
            src = row * stride
            out[row * tight : row * tight + tight] = data[src : src + tight]
        return bytes(out)

    def _detach_writer(self) -> None:
        with self._lock:
            writer, self._writer = self._writer, None
            self._handle_token = ""
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("preview egress writer close errored", exc_info=True)

    def shutdown(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._scene_source = None
        self._detach_writer()
