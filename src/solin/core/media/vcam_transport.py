"""Shared frame transport between Solin and the Windows virtual-camera filter.

Solin composites in libobs; the camera device that other applications see is a
separate COM component (``tools/solin-dshowcam``) living in *their* process. The
two meet through a file-backed mapping at a fixed machine-wide path.

Why a file and not named shared memory: the reader may run under a different
session or account than the writer (that was strictly true of the earlier Media
Foundation source, hosted by the frame server in session 0 as ``LocalService``).
``Local\\`` names are per-session, and creating a ``Global\\`` object needs
``SeCreateGlobalPrivilege``, which unelevated Solin does not hold. A path works
across both boundaries.

Layout: a 64-byte header followed by ``_SLOT_COUNT`` NV12 frame slots. A frame is
written into a slot that is not the published one and then published by storing
its index, so a reader mid-copy is never reading the slot being written.
"""

from __future__ import annotations

import logging
import mmap
import struct
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

FRAME_PATH = Path(r"C:\ProgramData\Solin\vcam-frame.bin")

#: Branded standby picture the DirectShow filter falls back to when Solin is not
#: producing frames. Without it the camera shows a black rectangle whenever Solin
#: is closed, which reads as a broken device rather than an idle one. Written as
#: raw NV12 at the filter's geometry so the filter needs no image decoder; the
#: filter loads it once and caches it (see frame_transport.h).
STANDBY_PATH = Path(r"C:\ProgramData\Solin\vcam-standby.nv12")

_MAGIC = 0x31435653  # 'SVC1'
_VERSION = 1
_HEADER_BYTES = 64
_SLOT_COUNT = 3
_FORMAT_NV12 = 0

# The filter advertises this geometry to consumers, so the writer must match it.
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3 // 2

_HEADER = struct.Struct("<8I2Q16s")

# libobs enums (video_format / video_range_type / video_colorspace).
_VIDEO_FORMAT_NV12 = 2
_VIDEO_RANGE_PARTIAL = 1
_VIDEO_CS_709 = 2


def rgb_to_nv12(rgb: bytes, width: int, height: int) -> bytes:
    """RGB888 -> NV12, studio swing.

    Pillow's YCbCr is full-range (JPEG); video wants 16-235 / 16-240, so the
    planes are rescaled. Done with Pillow point tables so the whole conversion
    stays in C rather than a Python loop over ~900k pixels.
    """
    from PIL import Image

    img = Image.frombytes("RGB", (width, height), rgb).convert("YCbCr")
    y, cb, cr = img.split()

    y = y.point(lambda v: 16 + (v * 219) // 255)
    cb = cb.point(lambda v: 128 + ((v - 128) * 224) // 255)
    cr = cr.point(lambda v: 128 + ((v - 128) * 224) // 255)

    half = (width // 2, height // 2)
    cb_small = cb.resize(half, Image.BILINEAR).tobytes()
    cr_small = cr.resize(half, Image.BILINEAR).tobytes()

    uv = bytearray(len(cb_small) * 2)
    uv[0::2] = cb_small  # NV12 interleaves U then V
    uv[1::2] = cr_small
    return y.tobytes() + bytes(uv)


def write_standby_frame(path: Path = STANDBY_PATH) -> bool:
    """Render Solin's branded idle screen and leave it on disk for the filter.

    Written once per run rather than continuously: it is the picture the camera
    shows while Solin is CLOSED, so it has to outlive the process. Cheap enough
    to refresh at startup, which also picks up branding changes.
    """
    if sys.platform != "win32":
        return False
    try:
        from ...projection.brand import render_idle_logo

        image = render_idle_logo(FRAME_WIDTH, FRAME_HEIGHT)
        rgb = image.convertToFormat(image.Format.Format_RGB888)
        stride = rgb.bytesPerLine()
        raw = bytes(rgb.constBits())
        row = FRAME_WIDTH * 3
        if stride != row:
            raw = b"".join(
                raw[y * stride : y * stride + row] for y in range(FRAME_HEIGHT)
            )
        nv12 = rgb_to_nv12(raw, FRAME_WIDTH, FRAME_HEIGHT)
        if len(nv12) != FRAME_BYTES:
            logger.warning(
                "standby frame is %d bytes, expected %d", len(nv12), FRAME_BYTES
            )
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and replace, so the filter never maps a
        # half-written file.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(nv12)
        tmp.replace(path)
    except Exception:  # noqa: BLE001 - branding must never block the camera
        logger.warning("Could not write the virtual-camera standby frame", exc_info=True)
        return False
    logger.info("Virtual camera standby frame written to %s", path)
    return True


class FrameTransport:
    """Publishes NV12 frames for the virtual-camera filter to pick up."""

    def __init__(self, path: Path = FRAME_PATH) -> None:
        self._path = path
        self._map: mmap.mmap | None = None
        self._file = None
        self._slot = 0
        self._index = 0

    @property
    def active(self) -> bool:
        return self._map is not None

    def open(self) -> bool:
        """Create/attach the frame file. False if it cannot be prepared."""
        if self._map is not None:
            return True
        total = _HEADER_BYTES + _SLOT_COUNT * FRAME_BYTES
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Pre-size it: the mapping cannot grow once a reader has attached.
            self._file = open(self._path, "r+b" if self._path.exists() else "w+b")
            self._file.truncate(total)
            self._file.flush()
            self._map = mmap.mmap(self._file.fileno(), total)
        except OSError:
            logger.warning("virtual camera frame transport unavailable at %s", self._path,
                           exc_info=True)
            self.close()
            return False

        self._write_header(frame_index=0, slot=0)
        self._widen_acl()
        return True

    def _write_header(self, *, frame_index: int, slot: int, timestamp: int = 0) -> None:
        assert self._map is not None
        self._map.seek(0)
        self._map.write(
            _HEADER.pack(_MAGIC, _VERSION, FRAME_WIDTH, FRAME_HEIGHT, _FORMAT_NV12,
                         FRAME_BYTES, _SLOT_COUNT, slot, frame_index, timestamp, b"")
        )

    def _widen_acl(self) -> None:
        """Grant LOCAL SERVICE read access.

        Only the Media Foundation source needs this — it is hosted by the Windows
        Camera Frame Server under that account. The DirectShow filter runs as the
        logged-on user and does not. Harmless either way, and cheap insurance if
        the MF backend is ever re-enabled.
        """
        if sys.platform != "win32":
            return
        try:
            subprocess.run(
                ["icacls", str(self._path), "/grant", "*S-1-5-19:(R)"],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("could not widen ACL on %s", self._path, exc_info=True)

    def publish(self, nv12: memoryview | bytes) -> bool:
        """Publish one NV12 frame. Wrong-sized frames are rejected, not padded."""
        if self._map is None:
            return False
        if len(nv12) != FRAME_BYTES:
            logger.debug("dropping frame with %d bytes (want %d)", len(nv12), FRAME_BYTES)
            return False

        slot = (self._slot + 1) % _SLOT_COUNT
        offset = _HEADER_BYTES + slot * FRAME_BYTES
        self._map[offset:offset + FRAME_BYTES] = nv12

        self._index += 1
        self._slot = slot
        self._write_header(frame_index=self._index, slot=slot)
        return True

    def close(self) -> None:
        if self._map is not None:
            try:
                self._map.close()
            except (OSError, ValueError):
                pass
            self._map = None
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


class RawVideoBridge:
    """Pumps a libobs video mix into a :class:`FrameTransport`.

    Binds to a specific ``video_t`` — the virtual camera's own view, not the
    global channel-0 mix — so the camera can show something different from what
    is being projected.
    """

    def __init__(self, transport: FrameTransport) -> None:
        self._transport = transport
        self._video = None
        self._callback = None  # cffi trampoline; libobs holds a raw pointer to it
        self._conversion = None  # cffi struct; libobs holds a raw pointer to it
        self._logged_error = False
        self._logged_geometry = False
        self.frames = 0

    def connect(self, video) -> bool:
        """Attach to ``video`` (a raw ``video_t*``). False if libobs refuses."""
        if self._callback is not None:
            return True
        try:
            from pylibobs._ffi import ffi, get_lib
        except Exception:  # noqa: BLE001 - optional dependency
            logger.debug("pylibobs ffi unavailable for the vcam bridge", exc_info=True)
            return False

        lib = get_lib()

        @ffi.callback("void(void*, struct video_data*)")
        def _on_frame(_param, frame):  # runs on a libobs thread — keep it short
            try:
                self._push(ffi, frame)
            except Exception:  # noqa: BLE001 - never raise into libobs
                # Log once: a silently swallowed failure here looks identical to
                # "libobs never called us", which is a very different bug.
                if not self._logged_error:
                    self._logged_error = True
                    logger.warning("vcam frame bridge failed", exc_info=True)

        # Ask libobs to scale to exactly the geometry the camera filter
        # advertises. The canvas is 1920x1080 by default while the filter is
        # 1280x720, and without this the frames arrive at canvas size — copying
        # FRAME_WIDTH bytes per row then silently CROPS the top-left quadrant
        # rather than scaling, which puts a centred logo in the lower right.
        conversion = ffi.new("struct video_scale_info *")
        conversion.format = _VIDEO_FORMAT_NV12
        conversion.width = FRAME_WIDTH
        conversion.height = FRAME_HEIGHT
        conversion.range = _VIDEO_RANGE_PARTIAL
        conversion.colorspace = _VIDEO_CS_709
        self._conversion = conversion  # libobs keeps the pointer; outlive the call

        ok = lib.video_output_connect(video, conversion, _on_frame, ffi.NULL)
        if not ok:
            logger.warning("video_output_connect refused the vcam mix")
            return False

        self._callback = _on_frame
        self._video = video
        self._lib = lib
        self._ffi = ffi
        return True

    def _push(self, ffi, frame) -> None:
        """Copy one libobs NV12 frame into the transport, honouring plane strides."""
        y_stride = frame.linesize[0]
        uv_stride = frame.linesize[1]
        if y_stride == 0:
            return

        # A stride NARROWER than the frame means libobs is handing us something
        # smaller than we asked for; copying FRAME_WIDTH bytes per row would read
        # into the next row and shear the image. A wider stride is just padding
        # and is handled below. Either way, say so once rather than shipping a
        # quietly wrong picture — that is how a 1080p canvas cropped into a 720p
        # frame put a centred logo in the corner.
        if y_stride < FRAME_WIDTH:
            if not self._logged_geometry:
                self._logged_geometry = True
                logger.warning(
                    "virtual camera mix is %d px wide, expected %d — dropping frames",
                    y_stride, FRAME_WIDTH,
                )
            return

        # libobs pads each row to an alignment boundary, so a stride wider than
        # the frame is normal and the planes must be repacked row by row.
        if y_stride == FRAME_WIDTH and uv_stride == FRAME_WIDTH:
            luma = FRAME_WIDTH * FRAME_HEIGHT
            buf = bytearray(FRAME_BYTES)
            buf[:luma] = ffi.buffer(frame.data[0], luma)
            buf[luma:] = ffi.buffer(frame.data[1], luma // 2)
        else:
            buf = bytearray(FRAME_BYTES)
            src_y = ffi.buffer(frame.data[0], y_stride * FRAME_HEIGHT)
            for row in range(FRAME_HEIGHT):
                start = row * y_stride
                buf[row * FRAME_WIDTH:(row + 1) * FRAME_WIDTH] = \
                    src_y[start:start + FRAME_WIDTH]
            base = FRAME_WIDTH * FRAME_HEIGHT
            src_uv = ffi.buffer(frame.data[1], uv_stride * (FRAME_HEIGHT // 2))
            for row in range(FRAME_HEIGHT // 2):
                start = row * uv_stride
                dst = base + row * FRAME_WIDTH
                buf[dst:dst + FRAME_WIDTH] = src_uv[start:start + FRAME_WIDTH]

        if self._transport.publish(buf):
            self.frames += 1

    def disconnect(self) -> None:
        if self._callback is None:
            return
        try:
            self._lib.video_output_disconnect(self._video, self._callback, self._ffi.NULL)
        except Exception:  # noqa: BLE001 - libobs boundary
            logger.debug("vcam bridge disconnect errored", exc_info=True)
        self._callback = None
        self._video = None
