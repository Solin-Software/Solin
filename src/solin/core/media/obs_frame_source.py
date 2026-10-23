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
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, TimeoutError
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

from cffi import FFI

# NOTE: pylibobs and PySide6 are imported LAZILY inside the methods that need
# them (as in obs_runtime) so this module — and the libobs sidecar, which pushes
# raw BGRA and never touches Qt — stay importable without those dependencies.

log = logging.getLogger(__name__)

_VIDEO_FORMAT_BGRA = 7  # enum video_format (matches pylibobs VideoFormat.BGRA)
_VIDEO_FORMAT_I420 = 1
# BT.709 limited range, as libobs' video_format_get_parameters() computes it (pylibobs does
# not expose that function): YUV->RGB matrix (row-major 4x4) and the range bounds.
_BT709_LIMITED_MATRIX = (
    1.164384, 0.000000, 1.792741, -0.972945,
    1.164384, -0.213249, -0.532909, 0.301483,
    1.164384, 2.112402, 0.000000, -1.133402,
    0.000000, 0.000000, 0.000000, 1.000000,
)
_LIMITED_RANGE_MIN = (16 / 255, 16 / 255, 16 / 255)
_LIMITED_RANGE_MAX = (235 / 255, 240 / 255, 240 / 255)
_OBS_SOURCE_VIDEO = 1 << 0  # obs_source_info.output_flags bit
_OBS_SOURCE_FRAME_LINEAR_ALPHA = 1 << 0
_FRAME_SOURCE_ID = "solin_frame_source"
_UPLOAD_POLL_SECONDS = 1 / 120

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


class _RenderThreadUploader:
    """Own render callbacks until OBS confirms removal, including on failed cleanup.

    The caller borrows the source until upload and unregistration finish. Only
    the texture upload runs on the native render thread; generation/deadline
    checks run on the waiting thread. A running upload always drains before its
    source can be released, even when a preparation becomes obsolete.
    """

    def __init__(
        self, runtime: Any, *, cancelled: threading.Event | None,
        check_current: Callable[[], None] | None,
    ) -> None:
        self._runtime = runtime
        self._cancelled = cancelled
        self._check_current = check_current
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Any] = []

    def _check(self) -> None:
        if self._closing.is_set() or (self._cancelled and self._cancelled.is_set()):
            raise CancelledError()
        if self._check_current is not None:
            self._check_current()

    def _unregister(self) -> None:
        # Keep each handle owned if native unregistration fails; the next
        # upload or close retries before freeing a referenced source.
        while self._callbacks:
            callback = self._callbacks[-1]
            self._runtime.ob.remove_main_render_callback(callback)
            self._callbacks.pop()

    def __call__(self, upload: Callable[[], None]) -> None:
        with self._lock:
            self._unregister()
            self._check()
            result: Future[None] = Future()
            claim_lock = threading.Lock()
            claimed = False

            def prepare(_width: int, _height: int) -> None:
                nonlocal claimed
                with claim_lock:
                    if claimed:
                        return
                    claimed = True
                    if not result.set_running_or_notify_cancel():
                        return
                try:
                    upload()
                except BaseException as error:  # noqa: BLE001 - forward native callback failure
                    result.set_exception(error)
                else:
                    result.set_result(None)

            callback = self._runtime.ob.add_main_render_callback(prepare)
            self._callbacks.append(callback)
            try:
                while True:
                    try:
                        result.result(timeout=_UPLOAD_POLL_SECONDS)
                        return
                    except TimeoutError:
                        if result.done():
                            # Preserve TimeoutError raised by the upload itself.
                            result.result()
                        self._check()
            except BaseException:  # noqa: BLE001 - cancel pending or drain running native upload
                if not result.cancel():
                    # Wait for native work without replacing the abort reason
                    # with an exception raised by the upload being retired.
                    result.exception()
                raise
            finally:
                # OBS's draw callback lock cannot be acquired from prepare().
                self._unregister()

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            self._unregister()


def create_frame_source(
    runtime, name: str = "solin-frame-source", *, cancelled: threading.Event | None = None,
    check_current: Callable[[], None] | None = None,
):
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
    uploader = _RenderThreadUploader(runtime, cancelled=cancelled, check_current=check_current)
    return ObsFrameSource(source, upload_first_frame=uploader, close_uploads=uploader.close)


class ObsFrameSource:
    """Wraps a ``solin_frame_source`` libobs Source; push QImages to display them."""

    def __init__(
        self, source, *, upload_first_frame: Callable[[Callable[[], None]], None],
        close_uploads: Callable[[], None] | None = None,
    ) -> None:
        self._source = source  # pylibobs Source
        self._upload_first_frame = upload_first_frame
        self._close_uploads = close_uploads

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
        presentation in the native render phase before a prepared scene can use it.
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
                # frame of the new presentation. Clear/preload keep their lock
                # order outside graphics. Upload runs on OBS's render thread so
                # its existing context cannot starve a worker behind readbacks.
                out_lib.obs_source_output_video(source._ptr, ffi.NULL)
                out_lib.obs_source_preload_video(source._ptr, frame_ptr)
                self._upload_first_frame(lambda: out_lib.obs_source_show_preloaded_video(source._ptr))
            else:
                out_lib.obs_source_output_video(source._ptr, frame_ptr)
            return True
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("obs_source_output_video failed", exc_info=True)
            return False

    def push_i420(self, data: bytes, width: int, height: int) -> bool:
        """Output one tightly packed I420 frame (BT.709, limited range), e.g. decoded
        WebRTC video. No Qt. libobs copies the planes."""
        source = self._source
        if source is None or width <= 0 or height <= 0 or width % 2 or height % 2:
            return False
        luma, chroma = width * height, (width // 2) * (height // 2)
        if len(data) < luma + 2 * chroma:
            return False
        from pylibobs._ffi import ffi, get_lib, is_alive  # type: ignore[import-not-found]

        if not is_alive():
            return False
        frame: Any = _frame_ffi.new("struct obs_source_frame *")
        base = _frame_ffi.cast("uint8_t *", _frame_ffi.from_buffer(data))
        frame.data[0] = base
        frame.data[1] = base + luma
        frame.data[2] = base + luma + chroma
        frame.linesize[0] = width
        frame.linesize[1] = width // 2
        frame.linesize[2] = width // 2
        frame.width = width
        frame.height = height
        frame.format = _VIDEO_FORMAT_I420
        frame.full_range = False
        for index, value in enumerate(_BT709_LIMITED_MATRIX):
            frame.color_matrix[index] = value
        for index in range(3):
            frame.color_range_min[index] = _LIMITED_RANGE_MIN[index]
            frame.color_range_max[index] = _LIMITED_RANGE_MAX[index]
        frame.timestamp = time.monotonic_ns()
        frame.flags = _OBS_SOURCE_FRAME_LINEAR_ALPHA  # video is opaque
        frame_ptr = ffi.cast("struct obs_source_frame *", int(_frame_ffi.cast("uintptr_t", frame)))
        try:
            out_lib: Any = get_lib()
            out_lib.obs_source_output_video(source._ptr, frame_ptr)
            return True
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("obs_source_output_video failed", exc_info=True)
            return False

    def clear(self) -> None:
        """Stop showing the last frame (the source renders nothing until the next one)."""
        source = self._source
        if source is None:
            return
        from pylibobs._ffi import ffi, get_lib, is_alive  # type: ignore[import-not-found]

        if not is_alive():
            return
        try:
            get_lib().obs_source_output_video(source._ptr, ffi.NULL)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not clear the frame source", exc_info=True)

    def release(self) -> None:
        if self._close_uploads is not None:
            # A failed unregistration keeps the handle and native source owned
            # so close() can be retried by the caller.
            self._close_uploads()
        src = self._source
        self._source = None
        if src is not None:
            try:
                src.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("frame source release failed", exc_info=True)


__all__ = ["ObsFrameSource", "create_frame_source"]
