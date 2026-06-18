from __future__ import annotations

from solin.core.integrations.camera_options import (
    CameraBackend,
    CameraOption,
    normalize_camera_backend,
)


def test_camera_option_key_and_virtual_detection():
    option = CameraOption(
        name="OBS Virtual Camera",
        label="OBS Virtual Camera",
        backend=CameraBackend.QT,
        cv_index=2,
        device_path="stable-id",
    )

    assert option.key == "qt:2"
    assert option.is_virtual is True


def test_normalize_camera_backend_maps_legacy_values():
    assert normalize_camera_backend("cv2") == "qt"
    assert normalize_camera_backend("cv2_dshow") == "qt"
    assert normalize_camera_backend("dshow") == "qt"
    assert normalize_camera_backend("qt") == "qt"
    assert normalize_camera_backend(" custom ") == "custom"
