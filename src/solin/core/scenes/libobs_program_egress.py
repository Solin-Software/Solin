"""Program-mirror egress for the scene-engine sidecar (libobs → operator tab).

The projection bar's live **Program** tab shows a thumbnail of the program
output — the main video mix, exactly what the projection windows and the virtual
camera show (including transitions). Unlike the editor preview (a specific edit
scene rendered on demand), the program is already libobs' main mix, so this
mirrors it with a raw video callback: obs converts+scales each rendered frame to
BGRA at the channel's size and this writes it into the cross-platform block the
app created (``SharedMemoryPreviewEgressController`` with the ``solin-program``
channel), which the app paints into the operator Program tab.

The callback is registered only while a program-egress descriptor is present
(the app supplies it only while the mirror is on), so obs does no conversion
work when the tab is not in use.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from solin.core.scenes.content_frame_consumer import SHARED_MEMORY_BGRA

log = logging.getLogger(__name__)


class LibobsProgramEgress:
    """Mirrors the program (main mix) into the app's program egress channel."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._writer: Any = None
        self._handle_token = ""
        self._width = 0
        self._height = 0
        self._callback: Any = None

    def configure(self, descriptor: object) -> None:
        """Attach (or detach) the mirror to the app's program egress block."""
        if not isinstance(descriptor, dict) or descriptor.get("transport") != SHARED_MEMORY_BGRA:
            self._teardown()
            return
        token = str(descriptor.get("handle_token") or "")
        width = int(descriptor.get("width") or 0)
        height = int(descriptor.get("height") or 0)
        if not token or width <= 0 or height <= 0:
            self._teardown()
            return
        with self._lock:
            if (
                self._writer is not None
                and self._handle_token == token
                and self._width == width
                and self._height == height
            ):
                return  # already mirroring into this exact block
        self._teardown()
        from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter

        ob = self._runtime.ob
        try:
            writer = SharedFrameChannelWriter(width, height, name=token, create=False)
        except Exception:  # noqa: BLE001 - the app may not have created the block yet
            log.warning("could not attach program egress writer to %r", token, exc_info=True)
            return
        try:
            callback = ob.add_raw_video_callback(
                self._on_frame, format=int(ob.VideoFormat.BGRA), width=width, height=height
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("could not register the program mirror callback", exc_info=True)
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("program egress writer close errored", exc_info=True)
            return
        with self._lock:
            self._writer = writer
            self._callback = callback
            self._handle_token = token
            self._width = width
            self._height = height

    def _on_frame(self, planes, linesizes, width, height, _fmt, _ts) -> None:
        # Runs on the obs video thread for every rendered program frame.
        with self._lock:
            writer = self._writer
            target_width = self._width
            target_height = self._height
        if writer is None or not planes:
            return
        stride = int(linesizes[0]) if linesizes else 0
        data = planes[0]
        if not data or stride == 0:
            return
        try:
            writer.write(self._tight(data, stride, target_width, target_height),
                         stride=target_width * 4)
        except Exception:  # noqa: BLE001 - shared-memory boundary
            log.debug("program egress write errored", exc_info=True)

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

    def _teardown(self) -> None:
        with self._lock:
            callback, self._callback = self._callback, None
            writer, self._writer = self._writer, None
            self._handle_token = ""
        if callback is not None:
            try:
                self._runtime.ob.remove_raw_video_callback(callback)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("program mirror callback removal errored", exc_info=True)
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort
                log.debug("program egress writer close errored", exc_info=True)

    def shutdown(self) -> None:
        self._teardown()
