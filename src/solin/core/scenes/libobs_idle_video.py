"""First-frame presentation and one demand-driven native idle decoder."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.media.obs_frame_source import create_frame_source
from solin.core.media.obs_runtime import MONITORING_NONE
from solin.core.media.obs_source_render import render_source_to_bgra
from solin.core.scenes.libobs_media_source import _hw_decode_enabled

log = logging.getLogger(__name__)


class IdleVideoError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(slots=True)
class _DecoderResources:
    source: Any
    disconnect: Callable[[], None] | None = None
    release_showing: Callable[[], None] | None = None

    def close(self) -> None:
        # Clear ownership only after successful cleanup. A failed disconnect
        # must keep its CFFI callback and source alive for a later retry.
        if self.disconnect is not None:
            self.disconnect()
            self.disconnect = None
        if self.release_showing is not None:
            self.release_showing()
            self.release_showing = None
        if self.source is not None:
            self.source.release()
            self.source = None


class LibobsIdleVideo:
    """Expose the immutable initial frame until a fresh decoder can play.

    The OBS FFmpeg source has no public seek-completion acknowledgement. A
    stopped presentation therefore retires its decoder in a worker and opens
    one inactive replacement. The private scene stays stable and displays its
    cached initial frame throughout; an immediate return never exposes the old
    texture. Only the first live consumer starts the prepared decoder. Repeated
    demand and additional live consumers share its clock.
    """

    def __init__(self, runtime: Any, name: str, path: str) -> None:
        self._runtime, self._name, self._path = runtime, name, path
        self._scene: Any = None
        self._item: Any = None
        self._still: Any = None
        self._decoder: Any = None
        self._resources: dict[int, _DecoderResources] = {}
        self._retiring: Any = None
        self._playing = False
        self._error_code = ""
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._preparing = 0
        self._closed = threading.Event()
        self._reset = threading.Event()
        self._worker: threading.Thread | None = None
        self._sequence = 0

    @property
    def source(self) -> Any:
        return self._scene.as_source()

    @property
    def decoder(self) -> Any:
        with self._lock:
            return self._decoder

    @property
    def error_code(self) -> str:
        # Read from native render callbacks without taking a control-thread lock.
        return self._error_code

    def _check(self, deadline_ms: int, check_current: Callable[[], None]) -> None:
        if self._closed.is_set():
            raise IdleVideoError("closed")
        check_current()
        if time.monotonic() * 1000 >= deadline_ms:
            raise IdleVideoError("deadline_exceeded")

    def _wait(self, deadline_ms: int) -> None:
        self._closed.wait(max(0.0, min(1 / 120, deadline_ms / 1000 - time.monotonic())))

    def _open(self, deadline_ms: int, check_current: Callable[[], None]) -> Any:
        """Decode the preload while inactive; its playback clock never advances."""
        self._check(deadline_ms, check_current)
        if not Path(self._path).is_file():
            raise IdleVideoError("idle_media_unavailable")
        self._sequence += 1
        settings = {
            "is_local_file": True, "local_file": "", "looping": True,
            "speed_percent": 100, "restart_on_activate": True,
            "close_when_inactive": False, "clear_on_media_end": False,
            "hw_decode": _hw_decode_enabled(),
        }
        source = self._runtime.ob.Source.create_private(
            "ffmpeg_source", f"{self._name}-decoder-{self._sequence}", settings,
        )
        if source is None:
            raise IdleVideoError("idle_source_unavailable")
        resources = _DecoderResources(source)
        with self._lock:
            self._resources[id(source)] = resources
        try:
            source.muted, source.volume, source.audio_mixers = True, 0.0, 0
            self._runtime.set_source_monitoring(source, MONITORING_NONE)
            updated = threading.Event()
            resources.disconnect = self._runtime.observe_source_updates(source, updated.set)
            source.update({**settings, "local_file": self._path})
            while True:
                self._check(deadline_ms, check_current)
                if updated.is_set() and self._runtime.prime_initial_source_video(source):
                    break
                self._wait(deadline_ms)
            # Neither attaching the prepared source nor hydrating a graph may
            # start it. Changing this flag leaves the inactive decoder intact.
            updated.clear()
            source.update({"restart_on_activate": False})
            while not updated.is_set():
                self._check(deadline_ms, check_current)
                self._wait(deadline_ms)
            source.media_play_pause(True)
            assert resources.disconnect is not None
            resources.disconnect()
            resources.disconnect = None
            return source
        except BaseException:  # noqa: BLE001 - rollback every native preparation
            self._release(source)
            raise

    def prepare(self, deadline_ms: int, check_current: Callable[[], None]) -> None:
        """Prepare and cache the actual first frame before publishing the scene."""
        with self._settled:
            self._check(deadline_ms, check_current)
            self._preparing += 1
        try:
            try:
                self._prepare(deadline_ms, check_current)
            finally:
                with self._settled:
                    self._preparing -= 1
                    self._settled.notify_all()
        except BaseException:  # noqa: BLE001 - dispose after leaving the preparation barrier
            self.close()
            raise

    def _prepare(self, deadline_ms: int, check_current: Callable[[], None]) -> None:
        decoder = self._open(deadline_ms, check_current)
        self._decoder = decoder
        self._check(deadline_ms, check_current)
        width, height = decoder.width, decoder.height
        frame = render_source_to_bgra(
            decoder, width, height, canvas_width=width, canvas_height=height,
        )
        self._check(deadline_ms, check_current)
        if frame is None:
            raise IdleVideoError("idle_media_invalid")
        self._still = create_frame_source(self._runtime, f"{self._name}-initial-frame")
        if self._still is None or not self._still.push_bgra(
            frame[0], width, height, frame[1], reset=True,
        ):
            raise IdleVideoError("idle_source_unavailable")
        self._scene = self._runtime.ob.Scene.create_private(f"{self._name}-presentation")
        if self._scene is None:
            raise IdleVideoError("idle_source_unavailable")
        self._replace(self._still.source)
        self._resources[id(decoder)].release_showing = self._runtime.retain_source_showing(decoder)
        self._check(deadline_ms, check_current)
        self._worker = threading.Thread(
            target=self._reprepare, name="idle-video-preparation", daemon=True,
        )
        self._worker.start()

    def _replace(self, source: Any | None) -> None:
        old, new = self._item, None

        def replace() -> None:
            nonlocal new
            try:
                if source is not None:
                    new = self._scene.add(source)
                    new.alignment, new.pos = 5, (0.0, 0.0)
                    new.bounds_type = self._runtime.ob.BoundsType.SCALE_INNER
                    new.bounds_alignment = 0
                    new.bounds = (float(self._runtime.video.width), float(self._runtime.video.height))
                if old is not None:
                    old.remove()
            except BaseException:  # noqa: BLE001 - atomic scene rollback
                if new is not None:
                    new.remove()
                raise

        try:
            self._runtime.atomic_scene_update(self._scene, replace)
        except BaseException:  # noqa: BLE001 - release detached candidate
            if new is not None:
                new.release()
            raise
        self._item = new
        if old is not None:
            old.release()

    def set_live(self, live: bool) -> None:
        """Reconcile on every render, never waiting for preparation or retirement."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._closed.is_set():
                return
            if live and not self._playing and self._decoder is not None:
                # This decoder has never played: its immutable native preload
                # is the first frame, and its async queue contains no old loop.
                self._replace(self._decoder)
                try:
                    self._decoder.media_restart()
                except BaseException:  # noqa: BLE001 - a possibly queued restart cannot be retried
                    self._replace(self._still.source)
                    self._retire_decoder()
                    raise
                self._playing = True
            elif not live and self._playing:
                assert self._decoder is not None
                self._replace(self._still.source)
                try:
                    self._decoder.media_play_pause(True)
                finally:
                    # Even a failed native pause must retire the hidden decoder.
                    self._retire_decoder()
                    self._playing = False
        finally:
            self._lock.release()

    def _retire_decoder(self) -> None:
        self._retiring, self._decoder = self._decoder, None
        self._reset.set()

    def _release(self, decoder: Any) -> None:
        if decoder is None:
            return
        with self._lock:
            resources = self._resources.get(id(decoder))
        if resources is not None:
            resources.close()
            with self._lock:
                self._resources.pop(id(decoder), None)

    def _reprepare(self) -> None:
        while True:
            self._reset.wait()
            self._reset.clear()
            with self._lock:
                retiring, self._retiring = self._retiring, None
            if retiring is not None:
                try:
                    self._release(retiring)
                except Exception:  # noqa: BLE001 - retain ownership for shutdown retry
                    with self._lock:
                        self._retiring = retiring
                        self._error_code = "idle_source_unavailable"
                    log.warning("Could not retire idle decoder", exc_info=True)
                    return
            if self._closed.is_set():
                return
            decoder = None
            try:
                decoder = self._open(int(time.monotonic() * 1000) + 5000, lambda: None)
                self._resources[id(decoder)].release_showing = (
                    self._runtime.retain_source_showing(decoder)
                )
                with self._lock:
                    if not self._closed.is_set():
                        self._decoder = decoder
                        self._error_code = ""
                        decoder = None
            except Exception as exc:  # noqa: BLE001 - preserve the immutable frame on failure
                if not self._closed.is_set():
                    with self._lock:
                        self._error_code = getattr(exc, "error_code", "idle_source_unavailable")
                    log.warning("Could not prepare idle video start", exc_info=True)
            finally:
                try:
                    self._release(decoder)
                except Exception:  # noqa: BLE001 - resources stay registered for close retry
                    self._error_code = "idle_source_unavailable"
                    log.warning("Could not release prepared idle decoder", exc_info=True)

    def close(self) -> None:
        """Drain native preparation before releasing scene, frame and decoder."""
        self._closed.set()
        self._reset.set()
        with self._settled:
            while self._preparing:
                self._settled.wait()
        with self._close_lock:
            self._close_resources()

    def _close_resources(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join()
        with self._lock:
            if self._scene is not None:
                self._replace(None)
                self._scene.release()
                self._scene = None
            for resources in tuple(self._resources.values()):
                self._release(resources.source)
            self._decoder, self._retiring = None, None
            if self._still is not None:
                self._still.release()
                self._still = None
