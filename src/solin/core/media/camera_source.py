"""Per-platform libobs camera capture source specification.

Shared by the projector (``program_driver.show_camera``) and the virtual camera
so both open a camera the same way — and, on the same device, the same source
instance can be reused rather than opening the device twice.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

#: The libobs capture source whose device list is authoritative per platform —
#: the same source Solin then opens, so an id from here always works.
#: ``(source id, device-list property)`` per platform. The property name differs
#: because each capture source names its device setting differently — the same
#: key :func:`camera_source_spec` writes.
_ENUM_SOURCE_ID = {
    "win32": ("dshow_input", "video_device_id"),
    "darwin": ("av_capture_input", "device"),
    "linux": ("v4l2_input", "device_id"),
}


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
        # libobs already reports ids as "<name>:<path>"; prefixing again would
        # yield "Cam:Cam:..." and fail to decode.
        if ":" in device_path or not device_name:
            vid = device_path
        else:
            vid = f"{device_name}:{device_path}"
        return "dshow_input", {"video_device_id": vid, "last_video_device_id": vid}
    if sys.platform == "darwin":
        return "av_capture_input", {"device": device_path}
    return "v4l2_input", {"device_id": device_path}


def libobs_cameras() -> list[tuple[str, str]]:
    """``(friendly name, device id)`` for every camera **libobs itself** can open.

    This is the *only* camera enumeration Solin does. Asking libobs makes the
    ids correct by construction: whatever comes back is, by definition, something
    the matching capture source accepts.

    Windows is why it matters. Qt's ``QMediaDevices`` enumerated through Media
    Foundation while libobs opens cameras through DirectShow — different
    namespaces, different id formats. Handing a Qt id to ``dshow_input`` produced
    ``DecodeDeviceId failed`` and a black picture, because the device was never
    opened.

    Returns ``[]`` when libobs is unavailable, so callers can degrade rather
    than lose the camera list entirely.
    """
    entry = next(
        (v for prefix, v in _ENUM_SOURCE_ID.items() if sys.platform.startswith(prefix)),
        None,
    )
    if entry is None:
        return []
    source_id, list_property = entry
    try:
        from .obs_runtime import obs_runtime

        runtime = obs_runtime()
        runtime.ensure_started()
        from pylibobs import Properties  # type: ignore[import-not-found]

        props = Properties.from_source_id(source_id)
    except Exception:  # noqa: BLE001 - libobs/optional-dependency boundary
        log.debug("libobs camera enumeration unavailable", exc_info=True)
        return []

    cameras: list[tuple[str, str]] = []
    try:
        for prop in props:
            if getattr(prop, "name", "") != list_property:
                continue
            for item in getattr(prop, "items", None) or []:
                name = (getattr(item, "name", "") or "").strip()
                value = getattr(item, "value", "") or ""
                if name and isinstance(value, str):
                    cameras.append((name, value))
            break
    except Exception:  # noqa: BLE001 - property walk is best-effort
        log.debug("libobs camera property walk failed", exc_info=True)
        return []
    log.info("libobs reports %d camera(s): %s", len(cameras), [n for n, _ in cameras])
    return cameras


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


__all__ = ["camera_source_spec", "camera_target", "libobs_cameras"]
