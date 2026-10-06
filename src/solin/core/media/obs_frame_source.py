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
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

from cffi import FFI

# NOTE: pylibobs and PySide6 are imported LAZILY inside the methods that need
# them (as in obs_runtime) so this module — and the libobs sidecar, which pushes
# raw BGRA and never touches Qt — stay importable without those dependencies.

log = logging.getLogger(__name__)

_VIDEO_FORMAT_BGRA = 7  # enum video_format (matches pylibobs VideoFormat.BGRA)
_OBS_SOURCE_VIDEO = 1 << 0  # obs_source_info.output_flags bit
_OBS_SOURCE_FRAME_LINEAR_ALPHA = 1 << 0
_FRAME_SOURCE_ID = "solin_frame_source"

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


def _ensure_frame_source_registered(runtime) -> None:
    """Make sure ``solin_frame_source`` exists, registering it in-process if needed.

    pylibobs can register an OBS async video source from Python, which removes the need for
    the solin-framesrc C plugin (no compiler, and it works the same on every platform). A
    plugin that already provides the id wins — the call is then a no-op.
    """
    register = getattr(runtime.ob, "register_frame_source", None)
    if register is None:
        return
    try:
        register(_FRAME_SOURCE_ID, "Solin Frame Source")
    except Exception:  # noqa: BLE001 - registration boundary; the guard below reports it
        log.debug("could not register the frame source in-process", exc_info=True)


def _is_real_frame_source(source) -> bool:
    """True when libobs actually registered ``solin_frame_source``.

    An unregistered id yields a placeholder with ``output_flags == 0``; every real video
    source carries ``OBS_SOURCE_VIDEO`` (image_source 0x8001, ffmpeg_source 0x2087).
    """
    try:
        from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

        lib: Any = get_lib()
        flags = int(lib.obs_source_get_output_flags(source._ptr))
    except Exception:  # noqa: BLE001 - libobs boundary; assume usable rather than break
        log.debug("could not read frame source output flags", exc_info=True)
        return True
    return bool(flags & _OBS_SOURCE_VIDEO)


def _is_opaque_bgra(data: bytes, width: int, height: int, stride: int) -> bool:
    """Inspect visible alpha bytes only, excluding row padding and spare capacity."""
    if stride == width * 4:
        return bytes(data[3:width * height * 4:4]).count(255) == width * height
    return all(
        bytes(data[row + 3:row + width * 4:4]).count(255) == width
        for row in range(0, height * stride, stride)
    )


def create_frame_source(runtime, name: str = "solin-frame-source"):
    """Create a ``solin_frame_source`` and wrap it. Returns None if the plugin
    is not registered (e.g. the module failed to load)."""
    _ensure_frame_source_registered(runtime)
    try:
        source = runtime.ob.Source.create(_FRAME_SOURCE_ID, name, {})
    except Exception:  # noqa: BLE001 - source-creation / plugin boundary
        builder = "build.bat" if sys.platform == "win32" else "build.sh"
        log.warning(
            "Could not create solin_frame_source — is the solin-framesrc plugin "
            "installed in the pylibobs bundle? (tools/obs-frame-source/%s)",
            builder,
            exc_info=True,
        )
        return None
    if source is None:
        return None
    if not _is_real_frame_source(source):
        # libobs does not fail an unknown source id: it returns a placeholder whose
        # output_flags are 0, so the except above never fires and every pushed frame is
        # silently discarded. Detect that here — otherwise images, timers and the live
        # tab all render nothing with no error anywhere.
        log.error(
            "The solin-framesrc libobs plugin is not installed, so Solin cannot display "
            "images, timers or the live tab. libobs returned a disabled placeholder for "
            "source id 'solin_frame_source'."
        )
        try:
            source.release()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            log.debug("could not release the placeholder frame source", exc_info=True)
        return None
    from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

    # Show pushed frames immediately (low latency); we feed real-time frames.
    try:
        lib: Any = get_lib()
        lib.obs_source_set_async_unbuffered(source._ptr, True)
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
        """Output one BGRA frame from a ``QImage`` (app side; needs Qt)."""
        from PySide6.QtGui import QImage

        if image is None or image.isNull():
            return
        # Qt Format_ARGB32 is 0xAARRGGBB → little-endian bytes B,G,R,A == BGRA.
        if image.format() != QImage.Format.Format_ARGB32:
            image = image.convertToFormat(QImage.Format.Format_ARGB32)
        width, height = image.width(), image.height()
        if width <= 0 or height <= 0:
            return
        # constBits() gives a copy owned by ``buf``; it must outlive the push call.
        self.push_bgra(bytes(image.constBits()), width, height, image.bytesPerLine())

    def push_bgra(
        self, data: bytes, width: int, height: int, stride: int, *, reset: bool = False,
    ) -> bool:
        """Output one raw BGRA frame. No Qt — used by the sidecar consumer.

        ``reset`` clears the async queue and uploads the first frame of a
        presentation synchronously before a prepared scene can use it.
        Subsequent frames use the low-latency async path. libobs copies the bytes.
        """
        source = self._source
        if source is None or not data or width <= 0 or height <= 0:
            return False
        if stride < width * 4 or len(data) < (height - 1) * stride + width * 4:
            return False
        from pylibobs._ffi import ffi, get_lib, is_alive  # type: ignore[import-not-found]

        if not is_alive():
            return False
        # cffi CData: its struct fields are only known at runtime, so type it Any.
        frame: Any = _frame_ffi.new("struct obs_source_frame *")
        frame.data[0] = _frame_ffi.cast("uint8_t *", _frame_ffi.from_buffer(data))
        frame.linesize[0] = int(stride)
        frame.width = int(width)
        frame.height = int(height)
        frame.format = _VIDEO_FORMAT_BGRA
        frame.full_range = True
        frame.timestamp = time.monotonic_ns()
        # With opaque alpha the blending spaces are equivalent. Declare linear
        # alpha to avoid OBS's redundant nonlinear color round trip per pixel;
        # frames containing transparency retain their nonlinear alpha semantics.
        frame.flags = _OBS_SOURCE_FRAME_LINEAR_ALPHA if _is_opaque_bgra(
            data, width, height, stride,
        ) else 0
        # Pass the struct to libobs by address (pylibobs's opaque ptr type).
        frame_ptr = ffi.cast("struct obs_source_frame *", int(_frame_ffi.cast("uintptr_t", frame)))
        try:
            out_lib: Any = get_lib()
            if reset:
                # Retire the async queue first, then synchronously upload the first
                # frame of the new presentation. show_preloaded_video owns the
                # graphics-context boundary internally; wrapping the whole sequence
                # here would invert its lock order against OBS's async video tick.
                out_lib.obs_source_output_video(source._ptr, ffi.NULL)
                out_lib.obs_source_preload_video(source._ptr, frame_ptr)
                out_lib.obs_source_show_preloaded_video(source._ptr)
            else:
                out_lib.obs_source_output_video(source._ptr, frame_ptr)
            return True
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("obs_source_output_video failed", exc_info=True)
            return False

    def release(self) -> None:
        src = self._source
        self._source = None
        if src is not None:
            try:
                src.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("frame source release failed", exc_info=True)


__all__ = ["ObsFrameSource", "create_frame_source"]
