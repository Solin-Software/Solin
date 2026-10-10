"""Presentation copy for stable native scene-engine error codes."""

from __future__ import annotations

from typing import Final, cast

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP


_TR_CONTEXT: Final = "SceneEngineStatus"
_VERSION_MISMATCH: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "Scene engine files do not match this Solin version.",
    ),
)
_SOURCE_UNAVAILABLE: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "A scene source is unavailable.",
    ),
)
_RESOURCE_LIMIT: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "This scene exceeds the available video resources.",
    ),
)
_OUTPUT_UNAVAILABLE: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "The scene output could not be started.",
    ),
)
_SCENE_CHANGED: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "Scenes changed while switching. Try again.",
    ),
)
_ENGINE_UNRESPONSIVE: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "The scene engine stopped responding.",
    ),
)
_INVALID_RESPONSE: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "The scene engine returned an invalid response.",
    ),
)
_GENERIC_FAILURE: Final[str] = cast(
    str,
    QT_TRANSLATE_NOOP(
        "SceneEngineStatus",
        "The scene engine could not complete this operation.",
    ),
)

_VERSION_CODES: Final = frozenset(
    {
        "invalid_scene_snapshot",
        "engine_protocol_error",
        "invalid_scene_graph",
    }
)
_SOURCE_CODES: Final = frozenset(
    {
        "source_disabled",
        "source_not_found",
        "source_not_implemented",
        "source_unavailable",
        "renderer_preparation_failed",
        "idle_media_unavailable",
        "idle_media_invalid",
        "idle_source_unavailable",
        "invalid_idle_screen",
    }
)
_RESOURCE_CODES: Final = frozenset({"media_resource_budget_exceeded"})
_OUTPUT_CODES: Final = frozenset(
    {
        "media_graph_unavailable",
        "media_graph_stopped",
        "frame_output_failed",
        "native_window_output_failed",
        "preview_output_failed",
        "program_output_failed",
        "virtual_camera_output_failed",
    }
)
_STALE_CODES: Final = frozenset(
    {
        "conflicting_document_revision",
        "duplicate_preparation_request",
        "preparation_mismatch",
        "preparation_not_found",
        "scene_not_found",
        "scene_reference_cycle",
        "stale_command_sequence",
        "stale_document_revision",
    }
)
_UNRESPONSIVE_CODES: Final = frozenset(
    {
        "engine_not_ready",
        "engine_process_failed",
        "engine_request_timed_out",
    }
)


def scene_engine_error_summary(error_code: str) -> str:
    """Translate a safe error code into concise, actionable interface copy."""

    if error_code in _VERSION_CODES:
        source = _VERSION_MISMATCH
    elif error_code in _SOURCE_CODES:
        source = _SOURCE_UNAVAILABLE
    elif error_code in _RESOURCE_CODES:
        source = _RESOURCE_LIMIT
    elif error_code in _OUTPUT_CODES:
        source = _OUTPUT_UNAVAILABLE
    elif error_code in _STALE_CODES:
        source = _SCENE_CHANGED
    elif error_code in _UNRESPONSIVE_CODES:
        source = _ENGINE_UNRESPONSIVE
    elif error_code == "unexpected_engine_response":
        source = _INVALID_RESPONSE
    else:
        source = _GENERIC_FAILURE
    return QCoreApplication.translate(_TR_CONTEXT, source)
