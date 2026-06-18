"""Pure OBS WebSocket v5 protocol payloads and parsers."""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from enum import IntEnum


class ObsOp(IntEnum):
    HELLO = 0
    IDENTIFY = 1
    IDENTIFIED = 2
    EVENT = 5
    REQUEST = 6
    RESPONSE = 7


EVT_SCENE_LIST_CHANGED = "SceneListChanged"
EVT_SCENE_CHANGED = "CurrentProgramSceneChanged"
EVT_RECORD_STATE_CHANGED = "RecordStateChanged"

_RECORDING_OUTPUT_STATES = frozenset(
    {
        "OBS_WEBSOCKET_OUTPUT_STARTED",
        "OBS_WEBSOCKET_OUTPUT_RESUMED",
    }
)


def make_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()
    ).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()
    ).decode()


def identify_payload(
    *,
    rpc_version: object,
    auth_info: Mapping[str, object] | None,
    password: str,
) -> dict[str, object]:
    identify_data: dict[str, object] = {"rpcVersion": _coerce_rpc_version(rpc_version)}

    if auth_info:
        if not password:
            raise ValueError("OBS requires a password but none was provided.")
        salt = str(auth_info.get("salt") or "")
        challenge = str(auth_info.get("challenge") or "")
        if not salt or not challenge:
            raise ValueError("OBS authentication challenge is incomplete.")
        identify_data["authentication"] = make_auth(password, salt, challenge)

    return {"op": ObsOp.IDENTIFY, "d": identify_data}


def request_payload(
    request_type: str,
    request_data: Mapping[str, object] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "requestType": request_type,
        "requestId": request_id or uuid.uuid4().hex[:8],
    }
    if request_data:
        data["requestData"] = dict(request_data)
    return {"op": ObsOp.REQUEST, "d": data}


def set_current_program_scene_payload(
    scene_name: str,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    return request_payload(
        "SetCurrentProgramScene",
        {"sceneName": scene_name},
        request_id=request_id,
    )


def request_id_from_payload(payload: Mapping[str, object]) -> str:
    data = payload.get("d")
    if not isinstance(data, Mapping):
        return ""
    return str(data.get("requestId") or "")


def is_response_for(message: Mapping[str, object], request_id: str) -> bool:
    data = message.get("d")
    return (
        message.get("op") == ObsOp.RESPONSE
        and isinstance(data, Mapping)
        and data.get("requestId") == request_id
    )


def parse_scene_names(response: Mapping[str, object]) -> list[str]:
    scenes = _response_data(response).get("scenes", [])
    if not isinstance(scenes, Sequence) or isinstance(scenes, (str, bytes, bytearray)):
        return []

    names: list[str] = []
    for scene in reversed(scenes):
        if not isinstance(scene, Mapping):
            continue
        scene_name = scene.get("sceneName")
        if isinstance(scene_name, str) and scene_name:
            names.append(scene_name)
    return names


def parse_current_scene(response: Mapping[str, object]) -> str:
    scene_name = _response_data(response).get("currentProgramSceneName", "")
    return scene_name if isinstance(scene_name, str) else ""


def parse_record_active(response: Mapping[str, object]) -> bool:
    return bool(_response_data(response).get("outputActive", False))


def event_type(message: Mapping[str, object]) -> str:
    data = message.get("d")
    if not isinstance(data, Mapping):
        return ""
    value = data.get("eventType")
    return value if isinstance(value, str) else ""


def event_data(message: Mapping[str, object]) -> Mapping[str, object]:
    data = message.get("d")
    if not isinstance(data, Mapping):
        return {}
    payload = data.get("eventData")
    return payload if isinstance(payload, Mapping) else {}


def is_recording_output_active(output_state: str) -> bool:
    return output_state in _RECORDING_OUTPUT_STATES


def _coerce_rpc_version(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _response_data(message: Mapping[str, object]) -> Mapping[str, object]:
    data = message.get("d")
    if not isinstance(data, Mapping):
        return {}
    response_data = data.get("responseData")
    return response_data if isinstance(response_data, Mapping) else {}

