"""Capture declarations shared by the release and development app bundles."""

from __future__ import annotations


def capture_usage_metadata() -> dict[str, str | bool]:
    return {
        "NSCameraUseContinuityCameraDeviceType": True,
        "NSCameraUsageDescription": "Solin uses your camera as a video source in scenes.",
        "NSMicrophoneUsageDescription": "Solin uses your microphone for audio capture and recording.",
    }
