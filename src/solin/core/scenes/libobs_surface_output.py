"""Deliver selected sources through the binding's native surface transport.

The binding owns graphics/IPC. This adapter owns only scene routing and demand,
using the same graph guard as native displays. The render callback never waits
for a scene rebuild or a slow receiving window.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


class LibobsSurfaceOutput:
    def __init__(self, runtime: Any, guard: threading.RLock,
                 source_resolver: Callable[[dict], Any]) -> None:
        self._runtime = runtime
        self._guard = guard
        self._resolve = source_resolver
        self._outputs: dict[str, tuple[Any, dict, tuple[int, int]]] = {}
        self._retired: list[Any] = []
        self._failed_endpoints: set[str] = set()
        self._closed = False
        self._callback: Any = None

    @property
    def render_targets(self) -> tuple[tuple[str, str], ...]:
        return tuple(("scene" if target.get("scene_id") else "projection",
                      str(target.get("scene_id", "")))
                     for _producer, target, _size in tuple(self._outputs.values()))

    def set_targets(self, targets: list[dict]) -> None:
        from pylibobs.macos_surfaces import SurfaceProducer

        if self._closed:
            raise RuntimeError("Surface output is shut down")
        size = self._runtime.video.width, self._runtime.video.height
        wanted = {str(target["surface_endpoint"]): target for target in targets
                  if target.get("visible", True)}
        failures: list[str] = []
        with self._guard:
            if not self._retired and not self._failed_endpoints and len(self._outputs) == len(wanted) and all(
                endpoint in self._outputs and self._outputs[endpoint][1:] == (target, size)
                for endpoint, target in wanted.items()
            ):
                return
            # Removal synchronizes with the native render callback before any
            # producer/borrowed source is released. Never remove under a callback.
            self._remove_callback()
            try:
                self._close_retired()
                # A receiving presenter has a unique endpoint for its lifetime.
                # Retain a failed identity until the GUI withdraws it, so an
                # unrelated geometry update cannot reconnect a dead receiver.
                self._failed_endpoints.intersection_update(wanted)
                for endpoint in tuple(self._outputs):
                    producer, _target, old_size = self._outputs[endpoint]
                    if endpoint not in wanted or size != old_size:
                        del self._outputs[endpoint]
                        producer.close()
                for endpoint, target in wanted.items():
                    if target.get("bus_id") != "media_windows" and not target.get("scene_id"):
                        raise ValueError("Surface output requires a projection bus or explicit scene")
                    if endpoint in self._failed_endpoints:
                        continue
                    current = self._outputs.get(endpoint)
                    try:
                        producer = current[0] if current is not None else SurfaceProducer(endpoint, *size)
                    except Exception as error:  # noqa: BLE001 - each receiving window is independent
                        failures.append(str(target.get("target_id", endpoint)))
                        if isinstance(error, ConnectionError):
                            log.debug("Surface receiver unavailable (%s)", target.get("target_id"))
                        else:
                            log.exception("Cannot connect surface output (%s)", target.get("target_id"))
                        continue
                    self._outputs[endpoint] = producer, target, size
            finally:
                if self._outputs:
                    self._callback = self._runtime.ob.add_main_render_callback(self._render)

        if failures:
            # Healthy windows remain active, but the control request must not
            # acknowledge failed targets as applied. Unlike a disconnected
            # established receiver, a failed connection may be retried.
            raise RuntimeError("Cannot connect surface targets: " + ", ".join(failures))

    def _remove_callback(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            self._runtime.ob.remove_main_render_callback(callback)

    def _render(self, *_canvas: int) -> None:
        if not self._outputs:
            return
        # libobs invokes this callback once per video frame. A second wall-clock
        # limiter drops valid frames when native scheduling has slight jitter.
        if not self._guard.acquire(blocking=False):
            return
        try:
            for endpoint, (producer, target, size) in tuple(self._outputs.items()):
                try:
                    source = self._resolve(target)
                    producer.render(source, canvas_width=size[0], canvas_height=size[1])
                except Exception as error:  # noqa: BLE001 - native render/transport boundary
                    del self._outputs[endpoint]
                    self._failed_endpoints.add(endpoint)
                    self._retired.append(producer)
                    if isinstance(error, ConnectionError):
                        log.debug("Surface receiver disconnected (%s)", target.get("target_id"))
                    else:
                        log.warning("Surface output failed (%s)", target.get("target_id"), exc_info=True)
        finally:
            self._guard.release()

    def _close_retired(self) -> None:
        while self._retired:
            self._retired[-1].close()
            self._retired.pop()

    def poll(self) -> None:
        """Release failed endpoints on the control thread, after rendering stops.

        The graphics callback only removes demand and transfers ownership to
        the retired list. Native callback removal can wait for that callback;
        doing it inside the callback itself would deadlock libobs.
        """
        if self._closed or not self._retired:
            return
        with self._guard:
            self._remove_callback()
            try:
                self._close_retired()
            finally:
                if self._outputs:
                    self._callback = self._runtime.ob.add_main_render_callback(self._render)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_callback()
        with self._guard:
            outputs, self._outputs = self._outputs, {}
            self._failed_endpoints.clear()
            self._retired.extend(producer for producer, _target, _size in outputs.values())
            self._close_retired()
