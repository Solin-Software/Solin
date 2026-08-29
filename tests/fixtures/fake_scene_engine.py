from __future__ import annotations

import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solin.core.scenes.ipc_protocol import (  # noqa: E402
    SceneIpcEnvelope,
    read_envelope,
    write_envelope,
)


def _respond(request: SceneIpcEnvelope, message_type: str, payload: dict[str, object]) -> None:
    write_envelope(
        sys.stdout.buffer,
        SceneIpcEnvelope(
            message_type=message_type,
            request_id=request.request_id,
            session_id=request.session_id,
            process_generation=request.process_generation,
            sequence=request.sequence,
            document_revision=request.document_revision,
            deadline_monotonic_ms=request.deadline_monotonic_ms,
            payload=payload,
        ),
    )


def _ack(request: SceneIpcEnvelope) -> None:
    _respond(
        request,
        "ack",
        {"applied": True, "error_code": "", "error_message": ""},
    )


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) >= 2 else "normal"
    marker = Path(sys.argv[2]) if len(sys.argv) >= 3 else None
    crash_this_generation = mode == "crash_first" and marker is not None and not marker.exists()
    recording_path = ""
    if mode == "expect_locked_plugins":
        plugin_path = os.environ.get("GST_PLUGIN_SYSTEM_PATH_1_0", "")
        registry_path = os.environ.get("GST_REGISTRY_1_0", "")
        if (
            os.environ.get("GST_PLUGIN_PATH_1_0") != ""
            or not plugin_path.endswith(os.path.join("lib", "gstreamer-1.0"))
            or not registry_path.endswith("registry.bin")
        ):
            return 73
    while request := read_envelope(sys.stdin.buffer):
        if request.process_generation != os.environ["SOLIN_MEDIA_ENGINE_PROCESS_GENERATION"]:
            return 70
        if request.message_type == "hello":
            _respond(
                request,
                "hello_ack",
                {
                    "local_cameras": False,
                    "rtsp_cameras": False,
                    "hardware_compositing": False,
                    "virtual_camera": False,
                    "d3d11_shared_textures": False,
                    "program_recording": True,
                    "audio_input_capture": True,
                    "system_audio_capture": True,
                },
            )
            if mode == "crash_after_hello":
                return 71
            if mode == "malformed_event":
                _respond(
                    request,
                    "source_health",
                    {
                        "source_id": "camera-1",
                        "status": "not-a-status",
                        "error_code": "",
                        "message": "",
                    },
                )
            if crash_this_generation:
                marker.write_text("crashed", encoding="utf-8")
                return 71
        elif request.message_type == "hydrate":
            if mode == "slow_hydrate":
                time.sleep(0.75)
            if mode == "reject_hydrate":
                _respond(
                    request,
                    "ack",
                    {
                        "applied": False,
                        "error_code": "invalid_scene_snapshot",
                        "error_message": "The scene snapshot was rejected",
                    },
                )
            else:
                _ack(request)
        elif request.message_type == "list_audio_devices":
            _respond(
                request,
                "audio_device_list",
                {
                    "supported": True,
                    "ready": True,
                    "generation": 1,
                    "devices": [
                        {
                            "device_id": "microphone-1",
                            "display_name": "Test microphone",
                            "direction": "input",
                            "is_default": True,
                        },
                        {
                            "device_id": "speakers-1",
                            "display_name": "Test speakers",
                            "direction": "output",
                            "is_default": True,
                        },
                    ],
                    "error_code": "",
                },
            )
        elif request.message_type == "start_program_recording":
            if set(request.payload) != {
                "path",
                "width",
                "height",
                "fps_numerator",
                "fps_denominator",
                "microphone",
                "system_audio",
            } or any(
                set(request.payload.get(field, {})) != {"mode", "device_id"}
                for field in ("microphone", "system_audio")
            ):
                _respond(
                    request,
                    "error",
                    {
                        "error_code": "invalid_recording_payload",
                        "error_message": "Recording payload did not match protocol v4",
                    },
                )
                continue
            recording_path = str(request.payload["path"])
            _respond(
                request,
                "program_recording_state",
                {
                    "status": "recording",
                    "path": recording_path,
                    "error_code": "",
                    "message": "",
                    "microphone_warning": "",
                    "system_audio_warning": "",
                    "dropped_frames": 0,
                    "duplicated_frames": 0,
                    "frame_feed_p95_ns": 0,
                },
            )
            _ack(request)
        elif request.message_type == "set_program_recording_audio":
            if set(request.payload) != {"microphone", "system_audio"} or any(
                set(request.payload.get(field, {})) != {"mode", "device_id"}
                for field in ("microphone", "system_audio")
            ):
                _respond(
                    request,
                    "error",
                    {
                        "error_code": "invalid_recording_audio_payload",
                        "error_message": "Recording audio payload did not match protocol v4",
                    },
                )
                continue
            _ack(request)
        elif request.message_type == "stop_program_recording":
            _respond(
                request,
                "program_recording_state",
                {
                    "status": "idle",
                    "path": recording_path,
                    "error_code": "",
                    "message": "",
                    "microphone_warning": "",
                    "system_audio_warning": "",
                    "dropped_frames": 0,
                    "duplicated_frames": 0,
                    "frame_feed_p95_ns": 0,
                },
            )
            _ack(request)
        elif request.message_type == "prepare_scene":
            if mode == "ignore_prepare":
                continue
            if set(request.payload) != {"bus_id", "scene_id", "transition"} or set(
                request.payload.get("transition", {})
            ) != {"kind", "duration_ms"}:
                _respond(
                    request,
                    "error",
                    {
                        "error_code": "invalid_prepare_payload",
                        "error_message": "Prepare payload did not match protocol v4",
                    },
                )
                continue
            if mode == "reject_prepare":
                _respond(
                    request,
                    "error",
                    {
                        "error_code": "source_unavailable",
                        "error_message": "Configured source is unavailable",
                    },
                )
                continue
            _respond(
                request,
                "scene_prepared",
                {
                    "bus_id": request.payload["bus_id"],
                    "scene_id": request.payload["scene_id"],
                    "preparation_token": f"prepared-{request.sequence}",
                    "transition": (
                        {"kind": "cut", "duration_ms": 0}
                        if mode == "transition_fallback"
                        else request.payload["transition"]
                    ),
                    "fallback_applied": mode == "transition_fallback",
                    "fallback_reason": (
                        "transition_pipeline_unavailable"
                        if mode == "transition_fallback"
                        else ""
                    ),
                },
            )
        elif request.message_type == "take_prepared":
            if set(request.payload) != {"bus_id", "scene_id", "preparation_token"}:
                _respond(
                    request,
                    "error",
                    {
                        "error_code": "invalid_take_payload",
                        "error_message": "Take payload did not match protocol v4",
                    },
                )
                continue
            _ack(request)
        elif request.message_type in {
            "preview_layer_geometry",
            "set_output_enabled",
            "set_render_enabled",
            "set_window_targets",
        }:
            _ack(request)
        elif request.message_type == "list_local_cameras":
            _respond(
                request,
                "local_camera_list",
                {
                    "supported": True,
                    "ready": True,
                    "generation": 1,
                    "devices": [
                        {
                            "device_id": "camera://device-1",
                            "display_name": "Test camera",
                            "software_device": False,
                            "formats": [
                                {
                                    "media_type": "video/x-raw",
                                    "pixel_format": "NV12",
                                    "width": 1920,
                                    "height": 1080,
                                    "fps_numerator": 30_000,
                                    "fps_denominator": 1_001,
                                }
                            ],
                        }
                    ],
                    "error_code": "",
                },
            )
        elif request.message_type == "ping":
            if mode == "ignore_heartbeat":
                continue
            _respond(request, "heartbeat", {"monotonic_ms": int(time.monotonic() * 1000)})
        elif request.message_type == "cancel_preparation":
            continue
        elif request.message_type == "stop":
            if mode == "ignore_stop":
                continue
            if mode == "slow_clean_stop" and marker is not None:
                time.sleep(0.2)
                marker.write_text("clean", encoding="utf-8")
            return 0
        else:
            return 72
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
