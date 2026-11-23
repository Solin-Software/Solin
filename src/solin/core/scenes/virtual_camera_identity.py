"""Default device names used to select scene output in meeting applications."""

from __future__ import annotations


def virtual_camera_device_name(platform: str) -> str:
    """Keep installed device names stable across interface languages."""
    return "Solin Virtual Camera" if platform == "win32" else "OBS Virtual Camera"
