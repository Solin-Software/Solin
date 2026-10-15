"""Observe negotiated camera frames without opening or copying another stream."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)
_FRAME_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class CameraCaptureState:
    generation: int
    status: str
    error_code: str = ""
    width: int = 0
    height: int = 0


class ObsCameraCaptureProbe:
    """Verify frames after each deferred update on the OBS video thread.

    The source is borrowed. Control-plane owners read immutable state and emit
    events themselves, keeping I/O and UI callbacks off the rendering thread.
    Hidden sources have no deadline: OBS need not tick an inactive input.
    """

    def __init__(
        self, runtime: Any, source: Any, *, width: int, height: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._source = source
        self._clock = monotonic
        self._lock = threading.Lock()
        self._expected = (width, height)
        self._state = CameraCaptureState(1, "starting")
        self._awaiting_update = False
        self._skip_frame = False
        self._last_timestamp = 0
        self._last_tick: float | None = None
        self._without_frame = 0.0
        self._frame: tuple[int, int, int] | None = None
        self._disconnect_update = runtime.observe_source_updates(source, self._updated)
        try:
            self._disconnect_video = runtime.observe_source_video(source, self._received)
        except Exception:  # noqa: BLE001 - release earlier subscription before propagating
            self._disconnect_update()
            raise
        try:
            self._callback = runtime.ob.add_main_render_callback(self._sample)
        except Exception:  # noqa: BLE001 - release native subscriptions before propagating
            self._disconnect_video()
            self._disconnect_update()
            raise

    @property
    def state(self) -> CameraCaptureState:
        with self._lock:
            return self._state

    def update(self, settings: dict[str, object], *, width: int, height: int) -> None:
        with self._lock:
            source = self._source
            if source is None:
                raise RuntimeError("Cannot update a closed camera capture probe")
            self._expected = (width, height)
            self._state = CameraCaptureState(self._state.generation + 1, "starting")
            self._awaiting_update = True
            self._last_tick = None
            self._without_frame = 0.0
        try:
            source.update(settings)
        except Exception:  # noqa: BLE001 - record native update failure before propagating
            with self._lock:
                self._awaiting_update = False
                self._state = CameraCaptureState(self._state.generation, "failed",
                                                 "camera_update_failed")
            raise

    def _updated(self) -> None:
        with self._lock:
            self._awaiting_update = False
            # async_tick selects before deferred_update in the same video tick.
            self._skip_frame = True

    def _received(self, width: int, height: int, timestamp: int) -> None:
        with self._lock:
            if self._source is not None:
                self._frame = width, height, timestamp

    def _sample(self, _width: int, _height: int) -> None:
        with self._lock:
            source = self._source
            if source is None:
                return
            # The old stream can keep producing after a rejected update. Only
            # a new explicit update may clear that rejection, even at the same size.
            if self._state.error_code == "camera_update_failed":
                return
            try:
                if not source.showing:
                    self._last_tick = None
                    return
                now = self._clock()
                if self._last_tick is not None:
                    self._without_frame += max(0.0, now - self._last_tick)
                self._last_tick = now
                frame = self._frame
                if self._skip_frame:
                    if frame is not None:
                        self._last_timestamp = frame[2]
                    self._skip_frame = False
                    frame = None
                if frame is not None and not self._awaiting_update:
                    width, height, timestamp = frame
                    if timestamp != self._last_timestamp:
                        self._last_timestamp = timestamp
                        expected = self._expected
                        if expected == (0, 0) or (width, height) == expected:
                            self._state = CameraCaptureState(self._state.generation, "ready",
                                                             width=width, height=height)
                            self._without_frame = 0.0
                            return
                        self._state = CameraCaptureState(self._state.generation, "starting",
                                                         width=width, height=height)
                if self._without_frame >= _FRAME_TIMEOUT_S:
                    state = self._state
                    code = ("camera_mode_mismatch" if state.width and self._expected != (0, 0)
                            and (state.width, state.height) != self._expected else "camera_no_frames")
                    self._state = CameraCaptureState(state.generation, "failed", code,
                                                     state.width, state.height)
            except Exception:  # noqa: BLE001 - no exception may cross a native callback
                if self._state.error_code != "camera_observation_failed":
                    log.warning("Could not observe camera frames", exc_info=True)
                self._state = CameraCaptureState(self._state.generation, "failed",
                                                 "camera_observation_failed")

    def close(self) -> None:
        with self._lock:
            self._source = None
            callback, self._callback = self._callback, None
            disconnect, self._disconnect_update = self._disconnect_update, None
            disconnect_video, self._disconnect_video = self._disconnect_video, None
        # Native unregistration can drain a callback; never hold its mutex here.
        try:
            if callback is not None:
                try:
                    self._runtime.ob.remove_main_render_callback(callback)
                except Exception:  # noqa: BLE001 - retain native callback ownership for retry
                    with self._lock:
                        self._callback = callback
                    raise
        finally:
            try:
                if disconnect_video is not None:
                    try:
                        disconnect_video()
                    except Exception:  # noqa: BLE001 - retain native callback ownership for retry
                        with self._lock:
                            self._disconnect_video = disconnect_video
                        raise
            finally:
                if disconnect is not None:
                    try:
                        disconnect()
                    except Exception:  # noqa: BLE001 - retain native callback ownership for retry
                        with self._lock:
                            self._disconnect_update = disconnect
                        raise
