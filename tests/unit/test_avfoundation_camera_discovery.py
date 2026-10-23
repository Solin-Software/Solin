from __future__ import annotations

import base64
from contextlib import nullcontext
from fractions import Fraction
import sys
from types import SimpleNamespace as NS

import pytest

from solin.core.scenes import avfoundation_camera_discovery as metadata
from solin.core.scenes.engine import LocalCameraModeQuery
from solin.core.scenes.model import CameraMediaType, LocalCameraConfig


def duration(value, scale):
    return NS(value=value, timescale=scale, flags=1, epoch=0)


def bounds(low, high):
    low, high = Fraction(low), Fraction(high)
    return NS(minFrameRate=lambda: float(low), maxFrameRate=lambda: float(high),
        minFrameDuration=lambda: duration(high.denominator, high.numerator),
        maxFrameDuration=lambda: duration(low.denominator, low.numerator))


def native_format(ranges, matrix="ITU_R_709_2", width=1920, height=1080):
    description = NS(width=width, height=height, subtype=int.from_bytes(b"420v", "big"),
        extensions={"CVImageBufferYCbCrMatrix": matrix})
    return NS(formatDescription=lambda: description, videoSupportedFrameRateRanges=lambda: ranges)


def device(formats, identity="native-ID"):
    return NS(uniqueID=lambda: identity, localizedName=lambda: "Camera", modelID=lambda: "USB Camera", transportType=lambda: int.from_bytes(b"usb ", "big"),
        formats=lambda: formats)


@pytest.fixture
def native(monkeypatch):
    state = NS(devices=[], status=0)
    av = NS(AVMediaTypeVideo="video", AVAuthorizationStatusDenied=2, AVAuthorizationStatusRestricted=1,
        AVCaptureDeviceTypeBuiltInWideAngleCamera="builtin", AVCaptureDeviceTypeExternalUnknown="external",
        AVCaptureDevicePositionUnspecified=0,
        AVCaptureDeviceDiscoverySession=NS(discoverySessionWithDeviceTypes_mediaType_position_=lambda *_: NS(devices=lambda: state.devices)),
        AVCaptureDevice=NS(authorizationStatusForMediaType_=lambda _: state.status,
                          devicesWithMediaType_=lambda _: state.devices))
    cm = NS(CMVideoFormatDescriptionGetDimensions=lambda desc: desc,
        CMFormatDescriptionGetMediaSubType=lambda desc: desc.subtype,
        CMFormatDescriptionGetExtensions=lambda desc: desc.extensions)
    # No capture classes are provided: metadata must only touch AVCaptureDevice.
    monkeypatch.setitem(sys.modules, "AVFoundation", av)
    monkeypatch.setitem(sys.modules, "CoreMedia", cm)
    monkeypatch.setitem(sys.modules, "objc", NS(autorelease_pool=nullcontext, lookUpClass=lambda _: NS(mainBundle=lambda: NS(infoDictionary=lambda: {}))))
    return state


def test_native_identity_preserves_colorspace_ranges_and_fractional_rates(native):
    native.devices = [device([
        native_format([bounds(25, 30), bounds(15, 20)]),
        native_format([bounds(Fraction(30000, 1001), Fraction(30000, 1001))], "ITU_R_601_4"),
    ])]
    found, = metadata.discover_avfoundation_cameras()
    assert found.device_id == "native-ID"
    assert found.probe.status == "ready"
    assert any((item.fps_numerator, item.fps_denominator) == (30000, 1001) for item in found.formats)
    assert len({item.native_format for item in found.formats}) == 2
    assert not any(item.supports(24, 1) for item in found.frame_rate_ranges)
    token = found.formats[0].native_format
    assert base64.b64decode(token).decode() == "1920x1080 15.000-20.00025.000-30.000 2 875704438"
    assert all(item.native_format for item in found.frame_rate_ranges)


def test_device_capability_failure_preserves_other_devices_and_identity(native):
    native.devices = [device([NS()], "broken"), device([native_format([bounds(30, 30)])], "good")]
    broken, good = metadata.discover_avfoundation_cameras()
    assert broken.device_id == "broken" and broken.probe.error_code == "capability_query_failed"
    assert good.probe.status == "ready" and good.formats


def test_empty_inventory_and_permission_denial_are_distinct(native):
    assert metadata.discover_avfoundation_cameras() == []
    native.status = 2
    with pytest.raises(metadata.AVFoundationDiscoveryError) as error:
        metadata.discover_avfoundation_cameras()
    assert error.value.error_code == "permission_denied"
    native.devices = [device([native_format([bounds(30, 30)])])]
    found, = metadata.discover_avfoundation_cameras()
    assert found.probe.error_code == "permission_denied" and not found.formats


def test_query_only_matches_requested_device_and_resolution(native):
    native.devices = [device([native_format([bounds(30, 30)])])]
    found, = metadata.discover_avfoundation_cameras(LocalCameraModeQuery("native-ID", 1280, 720))
    assert found.formats == () and found.probe.error_code == "no_formats"
    with pytest.raises(metadata.AVFoundationDiscoveryError) as error:
        metadata.discover_avfoundation_cameras(LocalCameraModeQuery("absent", 1920, 1080))
    assert error.value.error_code == "device_unavailable"


def test_invalid_native_duration_does_not_become_supported_fps():
    with pytest.raises(ValueError):
        metadata._duration(NS(value=1, timescale=30, flags=5, epoch=0))


def test_native_identity_survives_config_serialization_and_reset_to_auto():
    token = base64.b64encode(b"1920x1080 30.000-30.000 2 875704438").decode()
    config = LocalCameraConfig("native", 1920, 1080, 30000, 1001, CameraMediaType.RAW, "420v", native_format=token)
    assert LocalCameraConfig.from_record(config.to_record()) == config
    with pytest.raises(ValueError):
        LocalCameraConfig(native_format=token)


def test_software_camera_uses_native_transport_instead_of_display_name(native):
    camera = device([native_format([bounds(30, 30)])])
    camera.transportType = lambda: int.from_bytes(b"virt", "big")
    native.devices = [camera]
    found, = metadata.discover_avfoundation_cameras()
    assert found.software_device and found.display_name == "Camera"
