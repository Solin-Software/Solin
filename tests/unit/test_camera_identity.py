from __future__ import annotations

import pytest

from solin.core.media.camera_identity import local_camera_device_key


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_native_camera_identity_preserves_case_and_colons(platform: str) -> None:
    assert local_camera_device_key("Camera:Native-ID", platform=platform) == "Camera:Native-ID"
    assert local_camera_device_key("Native-ID", platform=platform) != local_camera_device_key(
        "native-id", platform=platform,
    )


def test_directshow_identity_ignores_friendly_name_and_native_path_case() -> None:
    first = r"Old camera:\\?\USB#VID_1234&PID_5678\global"
    renamed = r"New camera:\\?\usb#vid_1234&pid_5678\GLOBAL"
    native_path = first.split(":", 1)[1]

    assert local_camera_device_key(first, platform="win32") == local_camera_device_key(
        renamed, platform="win32",
    )
    assert local_camera_device_key(first, platform="win32") == local_camera_device_key(
        native_path, platform="win32",
    )


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_empty_camera_identity_remains_unbound(platform: str) -> None:
    assert local_camera_device_key("", platform=platform) == ""


@pytest.mark.parametrize("invalid", [None, 1, b"camera"])
def test_camera_identity_rejects_non_string_selectors(invalid: object) -> None:
    with pytest.raises(TypeError, match="identity"):
        local_camera_device_key(invalid)  # type: ignore[arg-type]
