"""Deliver selected sources through the binding's native surface transport.

The binding owns graphics/IPC. This adapter owns only scene routing and demand,
using the same graph guard as native displays. The render callback never waits
for a scene rebuild or a slow receiving window.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class LibobsSurfaceOutput:
    def __init__(self, runtime: Any, guard: threading.RLock,
                 source_resolver: Callable[[dict], Any]) -> None:
        self._runtime = runtime
        self._guard = guard
        self._resolve = source_resolver
        self._outputs: dict[str, tuple[Any, dict, tuple[int, int]]] = {}
        self._callback: Any = None
        self._next_frame_at = 0.0

    @property
    def render_targets(self) -> tuple[tuple[str, str], ...]:
        return tuple(("scene" if target.get("scene_id") else "projection",
                      str(target.get("scene_id", "")))
                     for _producer, target, _size in tuple(self._outputs.values()))

    def set_targets(self, targets: list[dict]) -> None:
        from pylibobs.macos_surfaces import SurfaceProducer

        size = self._runtime.video.width, self._runtime.video.height
        wanted = {str(target["surface_endpoint"]): target for target in targets
                  if target.get("visible", True)}
        with self._guard:
            # Removal synchronizes with the native render callback before any
            # producer/borrowed source is released. Never remove under a callback.
            self._remove_callback()
            try:
                for endpoint in tuple(self._outputs):
                    producer, _target, old_size = self._outputs[endpoint]
                    if endpoint not in wanted or size != old_size:
                        del self._outputs[endpoint]
                        producer.close()
                for endpoint, target in wanted.items():
                    if target.get("bus_id") != "media_windows" and not target.get("scene_id"):
                        raise ValueError("Surface output requires a projection bus or explicit scene")
                    current = self._outputs.get(endpoint)
                    try:
                        producer = current[0] if current is not None else SurfaceProducer(endpoint, *size)
                    except Exception:  # noqa: BLE001 - each receiving window is independent
                        log.exception("Cannot connect surface output (%s)", target.get("target_id"))
                        continue
                    self._outputs[endpoint] = producer, target, size
            finally:
                if self._outputs:
                    self._callback = self._runtime.ob.add_main_render_callback(self._render)

    def _remove_callback(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            self._runtime.ob.remove_main_render_callback(callback)

    def _render(self, *_canvas: int) -> None:
        now = time.monotonic()
        if now < self._next_frame_at or not self._guard.acquire(blocking=False):
            return
        try:
            self._next_frame_at = now + 1.0 / max(1, self._runtime.video.fps)
            for producer, target, size in tuple(self._outputs.values()):
                try:
                    source = self._resolve(target)
                    producer.render(source, canvas_width=size[0], canvas_height=size[1])
                except Exception:  # noqa: BLE001 - native render/transport boundary
                    log.exception("Surface output failed (%s)", target.get("target_id"))
        finally:
            self._guard.release()

    def shutdown(self) -> None:
        self._remove_callback()
        with self._guard:
            outputs, self._outputs = self._outputs, {}
            for producer, _target, _size in outputs.values():
                producer.close()
