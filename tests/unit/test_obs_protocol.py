from __future__ import annotations

import pytest

from solin.core.integrations.automation.obs_protocol import (
    EVT_RECORD_STATE_CHANGED,
    ObsOp,
    event_data,
    event_type,
    identify_payload,
    is_recording_output_active,
    is_response_for,
    make_auth,
    parse_current_scene,
    parse_record_active,
    parse_scene_names,
    request_id_from_payload,
    request_payload,
    set_current_program_scene_payload,
)


def test_make_auth_matches_obs_websocket_v5_algorithm():
    assert (
        make_auth("secret", "salt", "challenge")
        == "39cfhx7et2iyoMZvoQ6o3OPLNSKgtMmy48GQ7jnvsdE="
    )


def test_identify_payload_adds_authentication_when_required():
    payload = identify_payload(
        rpc_version="1",
        auth_info={"salt": "salt", "challenge": "challenge"},
        password="secret",
    )

    assert payload == {
        "op": ObsOp.IDENTIFY,
        "d": {
            "rpcVersion": 1,
            "authentication": "39cfhx7et2iyoMZvoQ6o3OPLNSKgtMmy48GQ7jnvsdE=",
        },
    }


def test_identify_payload_rejects_missing_password_when_auth_is_required():
    with pytest.raises(ValueError, match="password"):
        identify_payload(
            rpc_version=1,
            auth_info={"salt": "salt", "challenge": "challenge"},
            password="",
        )


def test_request_payload_uses_supplied_request_id_and_data():
    payload = request_payload(
        "SetCurrentProgramScene",
        {"sceneName": "Media"},
        request_id="abc12345",
    )

    assert payload == {
        "op": ObsOp.REQUEST,
        "d": {
            "requestType": "SetCurrentProgramScene",
            "requestId": "abc12345",
            "requestData": {"sceneName": "Media"},
        },
    }
    assert request_id_from_payload(payload) == "abc12345"


def test_set_current_program_scene_payload_wraps_scene_name():
    assert set_current_program_scene_payload("Camera", request_id="scene001") == {
        "op": ObsOp.REQUEST,
        "d": {
            "requestType": "SetCurrentProgramScene",
            "requestId": "scene001",
            "requestData": {"sceneName": "Camera"},
        },
    }


def test_response_matching_and_parsers():
    response = {
        "op": ObsOp.RESPONSE,
        "d": {
            "requestId": "req1",
            "responseData": {
                "scenes": [
                    {"sceneName": "Top"},
                    {"sceneName": "Middle"},
                    {"sceneName": "Bottom"},
                ],
                "currentProgramSceneName": "Middle",
                "outputActive": True,
            },
        },
    }

    assert is_response_for(response, "req1")
    assert not is_response_for(response, "other")
    assert parse_scene_names(response) == ["Bottom", "Middle", "Top"]
    assert parse_current_scene(response) == "Middle"
    assert parse_record_active(response) is True


def test_event_helpers_extract_payload_safely():
    event = {
        "op": ObsOp.EVENT,
        "d": {
            "eventType": EVT_RECORD_STATE_CHANGED,
            "eventData": {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
        },
    }

    assert event_type(event) == EVT_RECORD_STATE_CHANGED
    assert event_data(event)["outputState"] == "OBS_WEBSOCKET_OUTPUT_STARTED"
    assert is_recording_output_active("OBS_WEBSOCKET_OUTPUT_STARTED")
    assert is_recording_output_active("OBS_WEBSOCKET_OUTPUT_RESUMED")
    assert not is_recording_output_active("OBS_WEBSOCKET_OUTPUT_STOPPED")
