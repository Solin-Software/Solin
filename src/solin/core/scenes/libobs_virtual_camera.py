"""Virtual-camera output for the scene-engine sidecar.

The ``virtual_camera`` bus streams the program composite to a virtual-camera
device. On Linux libobs' ``virtualcam_output`` (linux-v4l2 plugin) writes to a
v4l2loopback device directly, so this binds that output to the main mix — the
program the scene builder composites on channel 0.

The Linux output is bound to the main video/audio (``ctx.get_video()`` /
``ctx.get_audio()``), i.e. it mirrors the main mix rather than a separate view.

Windows has no libobs ``virtualcam_output``; instead a registered Solin
DirectShow filter consumes the main mix from a per-user shared-memory frame ring
whose location it learns from a per-user named-pipe broker. On Windows this
delegates to :class:`~solin.core.scenes.libobs_windows_virtual_camera.LibobsWindowsVirtualCamera`,
which owns the broker + NV12 video producer in Python (see that module). The
shipped filter in ``native/virtual_camera`` shares this wire and frame contract.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from solin.core.scenes.output_health import OUTPUT_FRAME_TIMEOUT_S, OutputHealth, OutputHealthStatus

log = logging.getLogger(__name__)

_OUTPUT_KIND = "virtualcam_output"
_DEVICE_NAME = "Solin Virtual Camera"
_MAXIMUM_RECOVERY_ATTEMPTS = 3
_STABLE_OUTPUT_S = 10.0


class LibobsVirtualCamera:
    """Owns the virtual-camera output bound to the program composite."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._output: Any = None
        self._windows: Any = None  # LibobsWindowsVirtualCamera on win32
        self._enabled = False
        self._health = OutputHealth()
        self._recovery_attempts = 0
        self._next_retry_at = 0.0
        self._ready_since: float | None = None
        self._generation = 0
        self._last_frame_at = 0.0
        self._total_frames = 0

    @property
    def active(self) -> bool:
        return ((self._output is not None and self._output.active)
                or (self._windows is not None and self._windows.active))

    @property
    def health(self) -> OutputHealth:
        return self._health

    def start(self) -> bool:
        if self._enabled:
            try:
                return self.active
            except Exception:  # noqa: BLE001 - native output observation boundary
                log.warning("Could not observe virtual camera activation", exc_info=True)
                self._schedule_recovery("output_observation_failed")
                return False
        self._enabled = True
        self._recovery_attempts = 0
        return self._launch()

    def _launch(self) -> bool:
        self._last_frame_at = time.monotonic()
        self._ready_since = None
        self._total_frames = 0
        self._generation += 1
        started = self._start_windows() if sys.platform == "win32" else self._start_v4l2()
        if started:
            self._health = OutputHealth(OutputHealthStatus.STARTING, generation=self._generation)
        else:
            self._schedule_recovery("virtual_camera_unavailable")
        return started

    def poll(self) -> OutputHealth:
        """Observe progress and reconcile recovery on the sidecar control thread.

        The existing process heartbeat drives this method. Native lifecycle calls
        stay off the OBS video thread, and retries never outlive a disable request.
        """
        if not self._enabled or self._health.status is OutputHealthStatus.FAILED:
            return self._health
        now = time.monotonic()
        if self._health.status is OutputHealthStatus.RECOVERING:
            if now >= self._next_retry_at:
                self._recovery_attempts += 1
                self._launch()
            return self._health
        try:
            observed = self._observe(now)
        except Exception:  # noqa: BLE001 - native output observation boundary
            log.warning("Could not observe virtual camera output", exc_info=True)
            observed = OutputHealth(OutputHealthStatus.FAILED, "output_observation_failed")
        if observed.status is OutputHealthStatus.READY:
            if self._ready_since is None:
                self._ready_since = now
            if now - self._ready_since >= _STABLE_OUTPUT_S:
                self._recovery_attempts = 0
        else:
            self._ready_since = None
        if observed.status is OutputHealthStatus.FAILED:
            self._schedule_recovery(observed.error_code)
        else:
            self._health = OutputHealth(observed.status, observed.error_code, self._generation)
        return self._health

    def _observe(self, now: float) -> OutputHealth:
        if self._windows is not None:
            return self._windows.health
        output = self._output
        if output is None or not output.active:
            return OutputHealth(OutputHealthStatus.FAILED, "output_stopped")
        total_frames = int(output.total_frames)
        if total_frames != self._total_frames:
            self._total_frames = total_frames
            self._last_frame_at = now
        if now - self._last_frame_at >= OUTPUT_FRAME_TIMEOUT_S:
            return OutputHealth(OutputHealthStatus.FAILED, "output_no_frames")
        return OutputHealth(OutputHealthStatus.READY if total_frames else OutputHealthStatus.STARTING)

    def _schedule_recovery(self, error_code: str) -> None:
        self._stop_backend()
        self._ready_since = None
        exhausted = self._recovery_attempts >= _MAXIMUM_RECOVERY_ATTEMPTS
        self._health = OutputHealth(
            OutputHealthStatus.FAILED if exhausted else OutputHealthStatus.RECOVERING,
            error_code, self._generation,
        )
        self._next_retry_at = time.monotonic() + 0.5 * 2**self._recovery_attempts
        log.warning("Virtual camera output %s: code=%s generation=%d attempts=%d",
                    self._health.status, error_code, self._generation, self._recovery_attempts)

    def _start_windows(self) -> bool:
        try:
            from solin.core.scenes.libobs_windows_virtual_camera import (
                LibobsWindowsVirtualCamera,
            )

            windows = LibobsWindowsVirtualCamera(self._runtime)
            if not windows.start():
                return False
        except Exception:  # noqa: BLE001 - Windows vcam boundary
            log.warning("Could not start the Windows virtual camera", exc_info=True)
            return False
        self._windows = windows
        return True

    def _start_v4l2(self) -> bool:
        output = None
        try:
            ob = self._runtime.ob
            if _OUTPUT_KIND not in ob.enum_output_types():
                log.warning(
                    "%s is not available; the virtual camera needs a v4l2loopback "
                    "device (Linux)",
                    _OUTPUT_KIND,
                )
                return False
            context = self._runtime.context
            output = ob.Output.create(_OUTPUT_KIND, _DEVICE_NAME, {})
            output.set_media(context.get_video(), context.get_audio())
            if not output.start():
                log.warning("virtual camera output failed to start")
                failed, output = output, None
                failed.release()
                return False
        except Exception:  # noqa: BLE001 - output/libobs boundary
            log.warning("Could not start the virtual camera", exc_info=True)
            if output is not None:
                try:
                    output.release()
                except Exception:  # noqa: BLE001 - failed native creation cleanup
                    log.warning("Could not release the failed virtual camera output", exc_info=True)
            return False
        self._output = output
        return True

    def stop(self) -> None:
        self._enabled = False
        self._stop_backend()
        self._health = OutputHealth()
        self._ready_since = None
        self._next_retry_at = 0.0

    def _stop_backend(self) -> None:
        windows, self._windows = self._windows, None
        if windows is not None:
            try:
                windows.stop()
            except Exception:  # noqa: BLE001 - Windows vcam boundary
                log.debug("Windows virtual camera stop errored", exc_info=True)
        output, self._output = self._output, None
        if output is None:
            return
        try:
            # Virtual-camera sinks have no recording container to finalize.
            # Do not hold the sidecar heartbeat for the binding's default wait.
            output.stop(wait=False)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("virtual camera output stop errored", exc_info=True)
        try:
            output.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("virtual camera output release errored", exc_info=True)
