"""Per-platform libobs camera capture source specification.

Shared by the projector (``program_driver.show_camera``) and the virtual camera
so both open a camera the same way — and, on the same device, the same source
instance can be reused rather than opening the device twice.
"""

from __future__ import annotations

import sys


def camera_source_spec(device_path: str, device_name: str = "") -> tuple[str, dict]:
    """The libobs capture-source kind + settings for a camera, per platform.

    ``QCameraDevice.id()`` yields the platform-native device id — Linux:
    ``/dev/videoN``; Windows: the DirectShow device path; macOS: the AVFoundation
    ``uniqueID`` — and each OBS capture source expects it under a different key:

    * Linux  → ``v4l2_input``        ``device_id``
    * Windows→ ``dshow_input``       ``video_device_id`` (``"<name>:<path>"``)
    * macOS  → ``av_capture_input``  ``device``

    (Windows' dshow matches ``"<friendly name>:<device path>"``; the plain path is
    a best-effort fallback.) If the native source can't be created the caller
    keeps its fallback path, so an imperfect id degrades, it doesn't break.
    """
    if sys.platform == "win32":
        vid = f"{device_name}:{device_path}" if device_name else device_path
        return "dshow_input", {"video_device_id": vid, "last_video_device_id": vid}
    if sys.platform == "darwin":
        return "av_capture_input", {"device": device_path}
    return "v4l2_input", {"device_id": device_path}


def camera_target(option) -> tuple[str, str]:
    """Map a selected camera option to ``(device_path, device_name)`` for capture.

    Returns ``("", "")`` when there is no usable physical camera to open — none
    selected, a *virtual* camera (e.g. Solin's own vcam or OBS's — never feed the
    vcam its own output), or an option with no native device path. Callers pass the
    result straight to a source builder / :meth:`VirtualCamera.set_meeting_camera`.
    """
    if option is None:
        return "", ""
    device_path = getattr(option, "device_path", "") or ""
    if not device_path or getattr(option, "is_virtual", False):
        return "", ""
    return device_path, getattr(option, "name", "") or ""


__all__ = ["camera_source_spec", "camera_target"]
