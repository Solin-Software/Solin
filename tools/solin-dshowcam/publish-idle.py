"""Publish Solin's branded idle screen to the virtual camera.

This is the standby picture the virtual camera shows when nothing is being
projected — the JW badge centred on the brand background, straight from
``solin.projection.brand.render_idle_logo``, the same renderer the libobs vcam
uses for channel 0.

    python tools/solin-dshowcam/publish-idle.py [seconds]

The frame is static, so it is rendered and converted to NV12 exactly once and
then republished at 30fps; the per-frame cost is a single memcpy.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from solin.core.media.mfcam_win import (  # noqa: E402
    FRAME_HEIGHT,
    FRAME_WIDTH,
    FrameTransport,
)
from solin.projection.brand import render_idle_logo  # noqa: E402


def qimage_to_rgb_bytes(image: QImage) -> bytes:
    """Flatten a QImage to packed RGB888, dropping Qt's per-row padding."""
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height, stride = rgb.width(), rgb.height(), rgb.bytesPerLine()
    raw = bytes(rgb.constBits())
    row_bytes = width * 3
    if stride == row_bytes:
        return raw
    return b"".join(raw[y * stride : y * stride + row_bytes] for y in range(height))


def rgb_to_nv12(rgb: bytes, width: int, height: int) -> bytes:
    """RGB888 -> NV12, studio swing.

    Pillow's YCbCr is full-range (JPEG); video wants 16-235 / 16-240, so the
    planes are rescaled. Done with Pillow point tables so the whole conversion
    stays in C rather than a Python loop over 900k pixels.
    """
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


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - needed for QImage

    frame = rgb_to_nv12(
        qimage_to_rgb_bytes(render_idle_logo(FRAME_WIDTH, FRAME_HEIGHT)),
        FRAME_WIDTH,
        FRAME_HEIGHT,
    )
    print(f"rendered idle screen: {len(frame)} bytes NV12")

    transport = FrameTransport()
    if not transport.open():
        print("Could not open the frame transport.")
        return 1

    print(f"publishing Solin's idle screen for {duration:.0f}s")
    started = time.monotonic()
    published = 0
    try:
        while time.monotonic() - started < duration:
            if transport.publish(frame):
                published += 1
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        print(f"published {published} frames")
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
