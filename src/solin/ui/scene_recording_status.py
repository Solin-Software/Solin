"""Localized presentation copy for stable Program-recording status codes."""

from __future__ import annotations

from typing import Final, Literal, cast

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP


_TR_CONTEXT: Final = "SceneRecordingStatus"


def _source(text: str) -> str:
    return cast(str, QT_TRANSLATE_NOOP(_TR_CONTEXT, text))


_MICROPHONE_UNAVAILABLE: Final = _source(
    "The selected microphone is unavailable. Recording silence."
)
_SYSTEM_AUDIO_UNAVAILABLE: Final = _source(
    "The selected system audio output is unavailable. Recording silence."
)
_AUDIO_UNAVAILABLE: Final = _source(
    "The selected audio source is unavailable. Recording silence."
)
_INTERRUPTED: Final = _source("An unfinished recording was preserved.")
_FOLDER_UNAVAILABLE: Final = _source(
    "The recording folder is unavailable or does not have enough free space."
)
_ENCODER_UNAVAILABLE: Final = _source(
    "A compatible H.264/AAC encoder is unavailable."
)
_PROGRAM_UNAVAILABLE: Final = _source("The live output is not ready to record.")
_ENGINE_UNAVAILABLE: Final = _source("The scene engine stopped while recording.")
_FINALIZATION_FAILED: Final = _source(
    "The recording could not be finalized. Its partial file was preserved."
)
_GENERIC_FAILURE: Final = _source("Recording failed.")

_FOLDER_CODES: Final = frozenset(
    {
        "recording_path_unavailable",
        "recording_staging_path_exists",
        "recording_output_missing",
        "recording_output_rename_failed",
        "recording_disk_full",
        "recording_disk_space_low",
        "recording_output_failed",
    }
)
_ENCODER_CODES: Final = frozenset(
    {
        "program_recording_unsupported",
        "recording_encoder_unavailable",
        "recording_encoder_failed",
        "recording_pipeline_start_failed",
    }
)
_PROGRAM_CODES: Final = frozenset(
    {
        "program_not_ready",
        "program_render_unavailable",
        "recording_program_frame_timeout",
        "recording_video_format_unsupported",
        "recording_program_format_changed",
        "recording_gpu_sample_unavailable",
        "recording_video_buffer_failed",
        "recording_video_failed",
        "recording_video_push_failed",
    }
)
_ENGINE_CODES: Final = frozenset(
    {
        "engine_not_ready",
        "engine_restarted",
        "engine_unavailable",
        "engine_process_failed",
        "engine_request_timed_out",
    }
)
_FINALIZATION_CODES: Final = frozenset(
    {
        "recording_finalization_timeout",
        "recording_previous_session_cleanup_failed",
        "recording_stop_failed",
    }
)


def scene_recording_audio_warning(
    warning_code: str,
    direction: Literal["microphone", "system_audio"],
) -> str:
    """Turn a native warning code into safe, actionable interface copy."""

    if not warning_code:
        return ""
    if warning_code == "microphone_device_unavailable":
        source = _MICROPHONE_UNAVAILABLE
    elif warning_code == "system_audio_device_unavailable":
        source = _SYSTEM_AUDIO_UNAVAILABLE
    elif any(character.isspace() for character in warning_code):
        # Older engines may already provide presentation-ready copy.
        return warning_code
    elif direction == "microphone":
        source = _MICROPHONE_UNAVAILABLE
    elif direction == "system_audio":
        source = _SYSTEM_AUDIO_UNAVAILABLE
    else:  # pragma: no cover - Literal keeps production callers exhaustive
        source = _AUDIO_UNAVAILABLE
    return QCoreApplication.translate(_TR_CONTEXT, source)


def scene_recording_error_summary(error_code: str) -> str:
    """Turn a recording error code into concise interface copy."""

    if error_code == "recording_interrupted":
        source = _INTERRUPTED
    elif error_code in _FOLDER_CODES:
        source = _FOLDER_UNAVAILABLE
    elif error_code in _ENCODER_CODES:
        source = _ENCODER_UNAVAILABLE
    elif error_code in _PROGRAM_CODES:
        source = _PROGRAM_UNAVAILABLE
    elif error_code in _ENGINE_CODES:
        source = _ENGINE_UNAVAILABLE
    elif error_code in _FINALIZATION_CODES:
        source = _FINALIZATION_FAILED
    else:
        source = _GENERIC_FAILURE
    return QCoreApplication.translate(_TR_CONTEXT, source)
