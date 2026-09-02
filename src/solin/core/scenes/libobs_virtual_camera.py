"""Virtual-camera output for the scene-engine sidecar.

The ``virtual_camera`` bus streams the program composite to a virtual-camera
device. On Linux libobs' ``virtualcam_output`` (linux-v4l2 plugin) writes to a
v4l2loopback device directly, so this binds that output to the main mix — the
program the scene builder composites on channel 0.

The output is bound to the main video/audio (``ctx.get_video()`` /
``ctx.get_audio()``), i.e. it mirrors the main mix rather than a separate view.

Windows uses a Solin-owned DirectShow sink fed from the native engine; feeding it
from this libobs mix is a separate, on-hardware slice — here ``start`` simply
reports failure if ``virtualcam_output`` is not registered (as on Windows with
the bundled plugins), so the engine degrades gracefully.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_OUTPUT_KIND = "virtualcam_output"
_DEVICE_NAME = "Solin Virtual Camera"


class LibobsVirtualCamera:
    """Owns the virtual-camera output bound to the program composite."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._output: Any = None

    @property
    def active(self) -> bool:
        return self._output is not None

    def start(self) -> bool:
        if self._output is not None:
            return True
        ob = self._runtime.ob
        try:
            if _OUTPUT_KIND not in ob.enum_output_types():
                log.warning(
                    "%s is not available; the virtual camera needs a v4l2loopback "
                    "device (Linux) or the Solin DirectShow sink (Windows)",
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
