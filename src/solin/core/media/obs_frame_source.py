"""A libobs async video source you push ``QImage`` frames into.

Backs the live-browser projection. Rather than capture the browser's native X
window (which goes black when the operator switches panels — the window is
unmapped), we reuse the browser's off-screen frame stream (WebKitGTK/CDP
screencast, which keeps producing frames while hidden) and push those frames into
this source via ``obs_source_output_video``. libobs then composites and
crossfades it like any other scene — and a virtual-camera/recording would capture
it too — with none of the window-mapping fragility.

Requires the bundled ``solin_frame_source`` plugin (tools/obs-frame-source).
"""

from __future__ import annotations

import logging
import time

from cffi import FFI
from PySide6.QtGui import QImage

from pylibobs._ffi import ffi, get_lib, is_alive

log = logging.getLogger(__name__)

_VIDEO_FORMAT_BGRA = 7  # enum video_format (matches pylibobs VideoFormat.BGRA)

# pylibobs declares ``struct obs_source_frame`` as opaque (forward-declared), so
# it cannot allocate/fill one. Redeclare the exact libobs layout (obs.h /
# media-io-defs.h: MAX_AV_PLANES == 8) in a private FFI; we fill it here and pass
# it to obs_source_output_video by address. The field order/types MUST match
# libobs exactly — cffi computes the same C ABI layout the C compiler does.
_frame_ffi = FFI()
_frame_ffi.cdef(
    """
    struct obs_source_frame {
        uint8_t *data[8];
        uint32_t linesize[8];
        uint32_t width;
        uint32_t height;
        uint64_t timestamp;
        int format;
        float color_matrix[16];
        bool full_range;
        uint16_t max_luminance;
        float color_range_min[3];
        float color_range_max[3];
        bool flip;
        uint8_t flags;
        uint8_t trc;
        long refs;
        bool prev_frame;
    };
    """
)


def create_frame_source(runtime, name: str = "solin-frame-source"):
    """Create a ``solin_frame_source`` and wrap it. Returns None if the plugin
    is not registered (e.g. the module failed to load)."""
    try:
        source = runtime.ob.Source.create("solin_frame_source", name, {})
    except Exception:  # noqa: BLE001 - source-creation / plugin boundary
        log.warning(
            "Could not create solin_frame_source — is the solin-framesrc plugin "
            "installed in the pylibobs bundle? (tools/obs-frame-source/build.sh)",
            exc_info=True,
        )
        return None
    if source is None:
        return None
    # Show pushed frames immediately (low latency); we feed real-time frames.
    try:
        get_lib().obs_source_set_async_unbuffered(source._ptr, True)
    except Exception:  # noqa: BLE001 - optional, libobs boundary
        log.debug("obs_source_set_async_unbuffered unavailable", exc_info=True)
    return ObsFrameSource(source)


class ObsFrameSource:
    """Wraps a ``solin_frame_source`` libobs Source; push QImages to display them."""

    def __init__(self, source) -> None:
        self._source = source  # pylibobs Source

    @property
    def source(self):
        return self._source

    def push(self, image: QImage) -> None:
        """Output one BGRA frame. Copies synchronously inside libobs, so the
        temporary buffer only needs to outlive the call."""
        source = self._source
        if source is None or image is None or image.isNull() or not is_alive():
            return
        # Qt Format_ARGB32 is 0xAARRGGBB → little-endian bytes B,G,R,A == BGRA.
        if image.format() != QImage.Format.Format_ARGB32:
            image = image.convertToFormat(QImage.Format.Format_ARGB32)
        width, height = image.width(), image.height()
        if width <= 0 or height <= 0:
            return
        stride = image.bytesPerLine()
        buf = bytes(image.constBits())  # own copy; must outlive obs_source_output_video
        frame = _frame_ffi.new("struct obs_source_frame *")
        frame.data[0] = _frame_ffi.cast("uint8_t *", _frame_ffi.from_buffer(buf))
        frame.linesize[0] = stride
        frame.width = width
        frame.height = height
        frame.format = _VIDEO_FORMAT_BGRA
        frame.full_range = True
        frame.timestamp = time.monotonic_ns()
        # Pass the struct to libobs by address (pylibobs's opaque ptr type). libobs
        # copies it synchronously, so ``buf``/``frame`` only need to survive the call.
        frame_ptr = ffi.cast("struct obs_source_frame *", int(_frame_ffi.cast("uintptr_t", frame)))
        try:
            get_lib().obs_source_output_video(source._ptr, frame_ptr)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("obs_source_output_video failed", exc_info=True)

    def release(self) -> None:
        src = self._source
        self._source = None
        if src is not None:
            try:
                src.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("frame source release failed", exc_info=True)


__all__ = ["ObsFrameSource", "create_frame_source"]
