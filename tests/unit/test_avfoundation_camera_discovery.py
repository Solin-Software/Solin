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


@pytest.fixture
def legacy_capture(monkeypatch):
    from solin.core.media.camera_source import camera_source_spec

    monkeypatch.setattr(sys, "platform", "darwin")

    def capture(**changes):
        request = dict(width=1920, height=1080, fps_numerator=30, fps_denominator=1,
                       media_type="video/x-raw", pixel_format="420v")
        request.update(changes)
        return camera_source_spec("native-ID", **request)

    return capture


@pytest.mark.parametrize("fps", [Fraction(25), Fraction(30), Fraction(27), Fraction(30000, 1001)])
def test_saved_mode_resolves_unique_native_format_without_changing_rate(native, legacy_capture, fps):
    chosen = native_format([bounds(25, 30)])
    native.devices = [
        device([native_format([bounds(25, 30)], "ITU_R_601_4")], "another-device"),
        device([
            native_format([bounds(25, 30)], width=1280, height=720),
            native_format([bounds(50, 60)], "ITU_R_601_4"),
            chosen, chosen,  # Duplicate native metadata represents one identity.
        ]),
    ]
    kind, settings = legacy_capture(fps_numerator=fps.numerator, fps_denominator=fps.denominator)
    expected = metadata.discover_avfoundation_cameras(LocalCameraModeQuery("native-ID", 1920, 1080))[0]
    identity = next(item.native_format for item in expected.frame_rate_ranges
                    if item.supports(fps.numerator, fps.denominator))
    assert kind == "macos-avcapture"
    assert settings["device"] == "native-ID"
    assert settings["supported_format"] == identity
    assert settings["use_preset"] is False
    assert settings["frame_rate"] == {"numerator": fps.numerator, "denominator": fps.denominator}


@pytest.mark.parametrize("changes", [
    {"width": 1280, "height": 720}, {"fps_numerator": 31},
    {"pixel_format": "2vuy"}, {"media_type": "image/jpeg"},
])
def test_saved_mode_never_substitutes_an_unrelated_advertised_mode(native, legacy_capture, changes):
    native.devices = [device([native_format([bounds(25, 30)])])]
    with pytest.raises(metadata.AVFoundationDiscoveryError) as error:
        legacy_capture(**changes)
    assert error.value.error_code in {"format_unavailable", "no_formats"}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("difference", ["colorspace", "rate_ranges"])
def test_saved_mode_rejects_ambiguous_native_identities_regardless_of_order(
    native, legacy_capture, reverse, difference,
):
    other = (native_format([bounds(25, 30)], "ITU_R_601_4") if difference == "colorspace"
             else native_format([bounds(15, 30)]))
    formats = [native_format([bounds(25, 30)]),
               other]
    native.devices = [device(list(reversed(formats)) if reverse else formats)]
    with pytest.raises(metadata.AVFoundationDiscoveryError) as error:
        legacy_capture()
    assert error.value.error_code == "ambiguous_format"


@pytest.mark.parametrize("failure", ["device_unavailable", "permission_denied", "capability_query_failed"])
def test_saved_mode_preserves_metadata_failure_instead_of_falling_back(native, legacy_capture, failure):
    if failure == "permission_denied":
        native.status = 2
        native.devices = [device([native_format([bounds(30, 30)])])]
    elif failure == "capability_query_failed":
        native.devices = [device([NS()])]
    with pytest.raises(metadata.AVFoundationDiscoveryError) as error:
        legacy_capture()
    assert error.value.error_code == failure


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
