"""Native camera identity, independent of user-facing names and capture modes."""

from __future__ import annotations

import sys


def local_camera_device_key(device_id: str, *, platform: str | None = None) -> str:
    """Compare native IDs without changing the identifier sent to the plugin.

    DirectShow's selector may contain an escaped friendly name followed by a
    colon and the device path. Other backends expose case-sensitive opaque IDs.
    An empty selector is an unbound configuration, not a physical device.
    """
    if not isinstance(device_id, str):
        raise TypeError("Camera device identity must be a string")
    if (platform or sys.platform) == "win32":
        return device_id.split(":", 1)[-1].casefold()
    return device_id
