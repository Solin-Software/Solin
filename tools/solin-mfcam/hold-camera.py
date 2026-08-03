"""Hold "Solin Virtual Camera" open so it can be inspected in a real app.

The device exists only while a process owns the IMFVirtualCamera handle, so a
one-shot test leaves nothing for a browser to find. Run this, then pick
"Solin Virtual Camera" in Zoom / Chrome / Loom.

    python tools/solin-mfcam/hold-camera.py [seconds]

Frames published here are COLOURED. The media source falls back to a greyscale
test pattern when no producer is attached, so colour on screen means the frame
transport is genuinely carrying Solin-side frames across to the frame server.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from solin.core.media.mfcam_win import (  # noqa: E402
    FRAME_HEIGHT,
    FRAME_WIDTH,
    FrameTransport,
    MfVirtualCamera,
    available,
    is_source_registered,
)

Y_PLANE = FRAME_WIDTH * FRAME_HEIGHT


def make_frame(t: float) -> bytearray:
    """A vivid, obviously-moving NV12 frame: drifting hue plus a sweeping bar."""
    frame = bytearray(Y_PLANE * 3 // 2)

    bar = int((t * 0.25 % 1.0) * FRAME_HEIGHT)
    for row in range(FRAME_HEIGHT):
        luma = 90 + int(60 * math.sin((row / FRAME_HEIGHT + t * 0.3) * math.tau))
        if abs(row - bar) < 6:
            luma = 235
        start = row * FRAME_WIDTH
        frame[start:start + FRAME_WIDTH] = bytes([luma]) * FRAME_WIDTH

    # Interleaved U/V at half resolution — cycling gives an unmistakable colour shift.
    u = 128 + int(100 * math.sin(t * 0.7))
    v = 128 + int(100 * math.cos(t * 0.5))
    frame[Y_PLANE:] = bytes([max(16, min(240, u)), max(16, min(240, v))]) * (Y_PLANE // 4)
    return frame


def main() -> int:
    # --frames-only publishes frames without creating a Media Foundation camera.
    # That is what the DirectShow filter (tools/solin-dshowcam) wants: it is
    # already registered as a device, so all it needs is a producer.
    frames_only = "--frames-only" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    duration = float(args[0]) if args else 120.0

    if frames_only:
        transport = FrameTransport()
        if not transport.open():
            print("Could not open the frame transport.")
            return 1
        print(f"publishing frames for {duration:.0f}s (no MF camera)")
        started = time.monotonic()
        published = 0
        try:
            while (elapsed := time.monotonic() - started) < duration:
                if transport.publish(make_frame(elapsed)):
                    published += 1
                time.sleep(1 / 30)
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            print(f"published {published} frames")
            transport.close()
        return 0

    if not available():
        print("This needs Windows 11 (build 22000+).")
        return 1
    if not is_source_registered():
        print("The media source is not registered machine-wide. From an elevated prompt:")
        print(r"    regsvr32 /s C:\ProgramData\Solin\solin-mfcam.dll")
        return 1

    transport = FrameTransport()
    if not transport.open():
        print("Could not open the frame transport.")
        return 1

    camera = MfVirtualCamera()
    if not camera.start():
        print("Could not start the virtual camera (see log above).")
        transport.close()
        return 1

    print(f'"Solin Virtual Camera" is live for {duration:.0f}s — select it in your app.')
    print("Colour = frames from this process. Greyscale bars = the source's fallback.")
    started = time.monotonic()
    published = 0
    try:
        while (elapsed := time.monotonic() - started) < duration:
            if transport.publish(make_frame(elapsed)):
                published += 1
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        print(f"published {published} frames; removing camera")
        camera.stop()
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
