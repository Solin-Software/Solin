"""Preview egress for the scene-engine sidecar (libobs → app editor canvas).

The editor shows a live composite of the scene being edited (the EDITOR bus),
which may differ from the live Program. This renders that scene off-screen
over opaque black to BGRA via ``obs_source_render`` and writes it into the
cross-platform ``SHARED_MEMORY_BGRA`` block the app created and reads
(``SharedMemoryPreviewEgressController``). The app advertises the block's
``handle_token`` in the hydrate ``preview_egress`` descriptor; the sidecar
attaches to it as a writer.

Rendering runs in libobs' main render callback, which already owns the graphics
context. A competing worker can starve behind the main composite on a software
renderer. The source is borrowed from the scene graph; rendering and source
swaps are mutually excluded, and the sidecar clears it before rebuilding scenes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA

log = logging.getLogger(__name__)

_RENDER_INTERVAL_S = 1.0 / 30.0


class LibobsPreviewEgress:
    """Renders the edited scene into the app's preview channel."""

    def __init__(self, runtime: Any, *, before_render: Callable[[], bool] | None = None) -> None:
        self._runtime = runtime
        self._before_render = before_render
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._writer: Any = None
        self._handle_token = ""
        self._width = 0
        self._height = 0
        self._scene_source: Any = None
        self._enabled = False
        self._callback: Any = None
        self._render_to_bgra: Callable[..., Any] | None = None
        self._next_render_at = 0.0
        self._closed = False

    def configure(self, descriptor: object) -> None:
        """Attach (or re-attach) the writer to the app's egress block."""
        with self._lifecycle_lock:
            if not self._closed:
                self._configure(descriptor)

    def _configure(self, descriptor: object) -> None:
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
            self._next_render_at = 0.0
        from solin.core.media.obs_source_render import resolve_render_source_to_bgra

        try:
            self._render_to_bgra = resolve_render_source_to_bgra(
                before_render=self._before_render, opaque_background=True,
            )
            # OBS registration/removal takes its callback-list mutex. Never hold
            # the egress lock here: an executing callback may be waiting for it.
            self._callback = self._runtime.ob.add_main_render_callback(self._on_render)
        except Exception:  # noqa: BLE001 - libobs callback boundary
            log.warning("could not register preview render callback", exc_info=True)
            self._detach_writer()

    def set_scene_source(self, source: Any) -> None:
        """Set the borrowed scene source to render (None to stop rendering).

        Blocks on any in-flight render so a re-hydrate can then release the old
        scene safely.
        """
        with self._lock:
            self._scene_source = source
            self._next_render_at = 0.0

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._next_render_at = 0.0

    def _on_render(self, _cx: int, _cy: int) -> None:
        # The binding's callback trampoline catches exceptions without logging.
        # Own this boundary so a failed render/write remains diagnosable.
        try:
            with self._lock:
                writer = self._writer
                source = self._scene_source
                enabled = self._enabled
                width = self._width
                height = self._height
                render_to_bgra = self._render_to_bgra
                if writer is None or source is None or not enabled or render_to_bgra is None:
                    return
                now = time.monotonic()
                if now < self._next_render_at:
                    return
                self._next_render_at = now + _RENDER_INTERVAL_S
                canvas = self._runtime.video
                result = render_to_bgra(
                    source, width, height,
                    canvas_width=canvas.width, canvas_height=canvas.height,
                )
                if result is None:
                    return
                data, stride = result
                writer.write(self._tight(data, stride, width, height), stride=width * 4)
        except Exception:  # noqa: BLE001 - libobs render/shared-memory callback boundary
            log.warning("preview render or egress write errored", exc_info=True)

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
        # Removal waits for the native callback to finish, before the writer or
        # borrowed source can be disposed. The lifecycle lock serializes callers.
        callback = self._callback
        if callback is not None:
            self._runtime.ob.remove_main_render_callback(callback)
            self._callback = None
        with self._lock:
            writer, self._writer = self._writer, None
            self._handle_token = ""
            self._render_to_bgra = None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("preview egress writer close errored", exc_info=True)

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._detach_writer()
            with self._lock:
                self._scene_source = None
