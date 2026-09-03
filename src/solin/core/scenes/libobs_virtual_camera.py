"""Virtual-camera output for the scene-engine sidecar.

The ``virtual_camera`` bus streams the program composite to a virtual-camera
device. On Linux libobs' ``virtualcam_output`` (linux-v4l2 plugin) writes to a
v4l2loopback device directly, so this binds that output to the main mix — the
program the scene builder composites on channel 0.

The output is bound to the main video/audio (``ctx.get_video()`` /
``ctx.get_audio()``), i.e. it mirrors the main mix rather than a separate view.

Windows has no libobs ``virtualcam_output``; instead a registered Solin
DirectShow filter consumes the main mix from a per-user shared-memory frame ring
whose location it learns from a per-user named-pipe broker. On Windows this
delegates to :class:`~solin.core.scenes.libobs_windows_virtual_camera.LibobsWindowsVirtualCamera`,
which reimplements that broker + NV12 producer in Python (see that module). The
frame contract is identical to Solin's native engine, so the same installed
filter binds transparently.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

_OUTPUT_KIND = "virtualcam_output"
_DEVICE_NAME = "Solin Virtual Camera"


class LibobsVirtualCamera:
    """Owns the virtual-camera output bound to the program composite."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._output: Any = None
        self._windows: Any = None  # LibobsWindowsVirtualCamera on win32

    @property
    def active(self) -> bool:
        return self._output is not None or (self._windows is not None and self._windows.active)

    def start(self) -> bool:
        if self.active:
            return True
        if sys.platform == "win32":
            return self._start_windows()
        return self._start_v4l2()

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
        ob = self._runtime.ob
        try:
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
                output.release()
                return False
        except Exception:  # noqa: BLE001 - output/libobs boundary
            log.warning("Could not start the virtual camera", exc_info=True)
            return False
        self._output = output
        return True

    def stop(self) -> None:
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
            output.stop()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("virtual camera output stop errored", exc_info=True)
        try:
            output.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("virtual camera output release errored", exc_info=True)
