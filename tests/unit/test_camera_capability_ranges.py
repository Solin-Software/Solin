from fractions import Fraction

import pytest

from solin.core.scenes.engine import (
    LocalCameraDevice,
    LocalCameraProbe,
    LocalCameraProbeStatus,
    LocalVideoFrameInterval,
    LocalVideoFrameRateRange,
    LocalVideoResolutionRange,
)
from solin.core.scenes.model import CameraMediaType


def test_frame_interval_range_preserves_rational_step_and_fixed_resolution():
    rates = LocalVideoFrameRateRange(
        CameraMediaType.JPEG, "MJPG", 1920, 1080,
        LocalVideoFrameInterval(1, 30), LocalVideoFrameInterval(1, 10),
        LocalVideoFrameInterval(1, 30),
    )
    assert rates.supports(30, 1)
    assert rates.supports(15, 1)
    assert rates.supports(10, 1)
    assert not rates.supports(24, 1)
    assert not rates.supports(60, 1)
    assert not rates.supports(0, 1)
    assert rates.min_interval.seconds == Fraction(1, 30)


def test_continuous_rate_range_accepts_fractional_driver_rate():
    rates = LocalVideoFrameRateRange(
        CameraMediaType.RAW, "NV12", 1280, 720,
        LocalVideoFrameInterval(1, 30), LocalVideoFrameInterval(1, 15),
    )
    assert rates.supports(30_000, 1001)
    assert not rates.supports(31, 1)


def test_resolution_range_does_not_imply_any_frame_rates():
    sizes = LocalVideoResolutionRange(
        CameraMediaType.RAW, "YUYV", 320, 1920, 16, 240, 1080, 8,
    )
    assert sizes.supports(640, 480)
    assert not sizes.supports(641, 480)
    assert not sizes.supports(1920, 1081)
    device = LocalCameraDevice(
        "camera", "Camera", False, (),
        LocalCameraProbe(LocalCameraProbeStatus.READY, "v4l2"),
        resolution_ranges=(sizes,),
    )
    assert device.formats == ()
    assert device.frame_rate_ranges == ()


@pytest.mark.parametrize("numerator,denominator", [(0, 1), (1, 0), (2, 60), (True, 1)])
def test_interval_rejects_invalid_or_unreduced_rationals(numerator, denominator):
    with pytest.raises(ValueError):
        LocalVideoFrameInterval(numerator, denominator)


def test_reversed_ranges_are_rejected():
    with pytest.raises(ValueError):
        LocalVideoFrameRateRange(
            CameraMediaType.RAW, "NV12", 640, 480,
            LocalVideoFrameInterval(1, 15), LocalVideoFrameInterval(1, 30),
        )
    with pytest.raises(ValueError):
        LocalVideoResolutionRange(CameraMediaType.RAW, "NV12", 640, 320, 1, 240, 480, 1)
