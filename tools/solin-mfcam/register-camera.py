"""Register (or remove) "Solin Virtual Camera" permanently.

A persistent registration makes the device behave like OBS's virtual camera: it
stays in every application's camera list whether or not Solin is running, showing
the media source's fallback image until Solin feeds it.

That matters beyond convenience — **most applications enumerate cameras only at
startup**, so a device that only appears while Solin runs often will not show up
until the user restarts Chrome/Zoom/Teams.

Run ELEVATED (a system-lifetime camera is a machine-wide registration):

    python tools/solin-mfcam/register-camera.py            # register
    python tools/solin-mfcam/register-camera.py --remove   # unregister

This registers the *device*. The COM media source behind it must already be
registered machine-wide — see README.md.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from solin.core.media.mfcam_win import (  # noqa: E402
    FRIENDLY_NAME,
    MfVirtualCamera,
    available,
    is_source_registered,
)


def _elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def main() -> int:
    remove = "--remove" in sys.argv

    if not available():
        print("Needs Windows 11 (build 22000+).")
        return 1
    if not is_source_registered():
        print("The COM media source is not registered machine-wide. From an elevated prompt:")
        print(r"    regsvr32 /s C:\ProgramData\Solin\solin-mfcam.dll")
        return 1
    if not _elevated():
        print("This must run elevated — a system-lifetime camera is a machine-wide change.")
        print("Re-run it from an Administrator prompt.")
        return 1

    camera = MfVirtualCamera(persistent=True)
    if not camera.start():
        print("Could not create the virtual camera.")
        return 1

    if remove:
        camera.remove()
        print(f'"{FRIENDLY_NAME}" unregistered.')
    else:
        # Releasing the handle without Shutdown leaves a system-lifetime device
        # registered; that persistence is the entire point.
        camera.stop()
        print(f'"{FRIENDLY_NAME}" registered permanently.')
        print("It will now appear in camera lists even when Solin is not running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
