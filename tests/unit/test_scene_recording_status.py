from solin.ui.scene_recording_status import (
    scene_recording_audio_warning,
    scene_recording_error_summary,
)


def test_native_audio_warning_codes_become_actionable_copy() -> None:
    assert scene_recording_audio_warning(
        "microphone_device_unavailable",
        "microphone",
    ) == "The selected microphone is unavailable. Recording silence."
    assert scene_recording_audio_warning(
        "system_audio_device_unavailable",
        "system_audio",
    ) == "The selected system audio output is unavailable. Recording silence."


def test_unknown_machine_codes_do_not_leak_into_recording_ui() -> None:
    assert scene_recording_audio_warning(
        "unknown_device_code",
        "microphone",
    ) == "The selected microphone is unavailable. Recording silence."
    assert scene_recording_error_summary("unknown_recording_code") == (
        "Recording failed."
    )


def test_interrupted_recording_and_encoder_errors_have_specific_copy() -> None:
    assert scene_recording_error_summary("recording_interrupted") == (
        "An unfinished recording was preserved."
    )
    assert scene_recording_error_summary("recording_encoder_unavailable") == (
        "A compatible H.264/AAC encoder is unavailable."
    )
