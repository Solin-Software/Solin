"""ONVIF PTZ adapter with bounded SOAP, digest auth, and movement polling."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Protocol
from xml.etree import ElementTree

from solin.core.scenes.model import OnvifPtzBinding, PtzProtocol
from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzCancelledError,
    PtzControlKind,
    PtzControlRequest,
    PtzCredentialError,
    PtzCredentialResolver,
    PtzCredentials,
    PtzExecutionError,
    PtzRecallRequest,
)


_SOAP_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
_PTZ_NAMESPACE = "http://www.onvif.org/ver20/ptz/wsdl"
_SCHEMA_NAMESPACE = "http://www.onvif.org/ver10/schema"
_WSSE_NAMESPACE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
_WSU_NAMESPACE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
_PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_ENCODING_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
_MAXIMUM_RESPONSE_BYTES = 1024 * 1024
_POLL_INTERVAL_SECONDS = 0.1
_REQUIRED_IDLE_POLLS = 2

_ACTION_GOTO_PRESET = f"{_PTZ_NAMESPACE}/GotoPreset"
_ACTION_ABSOLUTE_MOVE = f"{_PTZ_NAMESPACE}/AbsoluteMove"
_ACTION_GET_STATUS = f"{_PTZ_NAMESPACE}/GetStatus"
_ACTION_CONTINUOUS_MOVE = f"{_PTZ_NAMESPACE}/ContinuousMove"
_ACTION_STOP = f"{_PTZ_NAMESPACE}/Stop"
_ACTION_SET_PRESET = f"{_PTZ_NAMESPACE}/SetPreset"


@dataclass(frozen=True, slots=True)
class OnvifHttpResponse:
    status: int
    body: bytes

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("ONVIF HTTP status is invalid")
        if len(self.body) > _MAXIMUM_RESPONSE_BYTES:
            raise ValueError("ONVIF response is too large")


class OnvifTransport(Protocol):
    def call(
        self,
        *,
        endpoint: str,
        action: str,
        payload: bytes,
        credentials: PtzCredentials | None,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> OnvifHttpResponse: ...


class AiohttpOnvifTransport:
    """Per-call transport; total timeout includes DNS, connect, send, and receive."""

    def call(
        self,
        *,
        endpoint: str,
        action: str,
        payload: bytes,
        credentials: PtzCredentials | None,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> OnvifHttpResponse:
        if timeout_seconds <= 0:
            raise TimeoutError
        return asyncio.run(
            self._call(
                endpoint=endpoint,
                action=action,
                payload=payload,
                credentials=credentials,
                timeout_seconds=timeout_seconds,
                cancellation=cancellation,
            )
        )

    async def _call(
        self,
        *,
        endpoint: str,
        action: str,
        payload: bytes,
        credentials: PtzCredentials | None,
        timeout_seconds: float,
        cancellation: PtzCancellation,
    ) -> OnvifHttpResponse:
        import aiohttp

        middlewares = (
            (aiohttp.DigestAuthMiddleware(credentials.username, credentials.password),)
            if credentials is not None
            else ()
        )
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        headers = {
            "Accept": "application/soap+xml",
            "Accept-Encoding": "identity",
            "Content-Type": (
                f'application/soap+xml; charset="utf-8"; action="{action}"'
            ),
        }

        async def perform() -> OnvifHttpResponse:
            async with aiohttp.ClientSession(
                timeout=timeout,
                middlewares=middlewares,
                trust_env=False,
                auto_decompress=False,
            ) as session:
                async with session.post(
                    endpoint,
                    data=payload,
                    headers=headers,
                    allow_redirects=False,
                    ssl=True,
                ) as response:
                    body = await response.content.read(_MAXIMUM_RESPONSE_BYTES + 1)
                    if len(body) > _MAXIMUM_RESPONSE_BYTES:
                        raise PtzExecutionError("ptz_onvif_response_too_large")
                    return OnvifHttpResponse(response.status, body)

        task = asyncio.create_task(perform())
        try:
            while not task.done():
                if cancellation.cancelled:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise PtzCancelledError
                await asyncio.wait({task}, timeout=0.05)
            return task.result()
        except TimeoutError:
            raise
        except PtzCancelledError:
            raise
        except aiohttp.ClientConnectorCertificateError as error:
            raise PtzExecutionError("ptz_tls_verification_failed") from error
        except aiohttp.ClientSSLError as error:
            raise PtzExecutionError("ptz_tls_failed") from error
        except aiohttp.ClientError as error:
            raise PtzExecutionError("ptz_network_unavailable") from error


class OnvifPtzAdapter:
    protocol = PtzProtocol.ONVIF

    def __init__(
        self,
        *,
        transport: OnvifTransport,
        credentials: PtzCredentialResolver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._clock = clock

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, OnvifPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        if not binding.profile_token:
            raise PtzExecutionError("ptz_profile_token_required")
        credentials = self._resolve_credentials(binding.credential_ref)
        if request.preset.remote_token:
            action = _ACTION_GOTO_PRESET
            operation = _operation(
                "GotoPreset",
                profile_token=binding.profile_token,
                preset_token=request.preset.remote_token,
            )
        elif request.preset.position is not None:
            action = _ACTION_ABSOLUTE_MOVE
            operation = _absolute_move_operation(
                binding.profile_token,
                pan=request.preset.position.pan,
                tilt=request.preset.position.tilt,
                zoom=request.preset.position.zoom,
            )
        else:
            raise PtzExecutionError("ptz_preset_target_required")
        self._invoke(
            request,
            cancellation,
            binding,
            credentials,
            action,
            operation,
        )
        idle_polls = 0
        while idle_polls < _REQUIRED_IDLE_POLLS:
            if cancellation.wait(
                min(_POLL_INTERVAL_SECONDS, request.remaining_seconds(self._clock))
            ):
                raise PtzCancelledError
            status_response = self._invoke(
                request,
                cancellation,
                binding,
                credentials,
                _ACTION_GET_STATUS,
                _operation("GetStatus", profile_token=binding.profile_token),
            )
            if _movement_is_idle(status_response):
                idle_polls += 1
            else:
                idle_polls = 0

    def control(
        self,
        request: PtzControlRequest,
        cancellation: PtzCancellation,
    ) -> None:
        binding = request.binding
        if not isinstance(binding, OnvifPtzBinding):
            raise PtzExecutionError("ptz_binding_mismatch")
        if not binding.profile_token:
            raise PtzExecutionError("ptz_profile_token_required")
        credentials = self._resolve_credentials(binding.credential_ref)
        if request.kind is PtzControlKind.MOVE:
            action = _ACTION_CONTINUOUS_MOVE
            operation = _continuous_move_operation(
                binding.profile_token,
                pan=request.pan,
                tilt=request.tilt,
                zoom=request.zoom,
            )
        elif request.kind is PtzControlKind.STOP:
            action = _ACTION_STOP
            operation = _stop_operation(binding.profile_token)
        else:
            preset = request.preset
            if preset is None or not preset.remote_token:
                raise PtzExecutionError("ptz_preset_target_required")
            action = _ACTION_SET_PRESET
            operation = _set_preset_operation(
                binding.profile_token,
                preset_name=preset.name,
                preset_token=preset.remote_token,
            )
        self._invoke(
            request,
            cancellation,
            binding,
            credentials,
            action,
            operation,
        )

    def _resolve_credentials(self, credential_ref: str) -> PtzCredentials | None:
        if not credential_ref:
            return None
        if self._credentials is None:
            raise PtzExecutionError("ptz_credentials_unavailable")
        try:
            resolved = self._credentials.resolve(credential_ref)
        except PtzCredentialError as error:
            raise PtzExecutionError(error.error_code) from error
        if resolved is None:
            raise PtzExecutionError("ptz_credentials_not_found")
        return resolved

    def _invoke(
        self,
        request: PtzRecallRequest | PtzControlRequest,
        cancellation: PtzCancellation,
        binding: OnvifPtzBinding,
        credentials: PtzCredentials | None,
        action: str,
        operation: ElementTree.Element,
    ) -> bytes:
        remaining = request.remaining_seconds(self._clock)
        if remaining <= 0:
            raise TimeoutError
        response = self._transport.call(
            endpoint=binding.endpoint,
            action=action,
            payload=_soap_envelope(operation, credentials),
            credentials=credentials,
            timeout_seconds=remaining,
            cancellation=cancellation,
        )
        _raise_for_response(response)
        return response.body


def _operation(
    name: str,
    *,
    profile_token: str,
    preset_token: str = "",
) -> ElementTree.Element:
    operation = ElementTree.Element(_tag(_PTZ_NAMESPACE, name))
    profile = ElementTree.SubElement(
        operation,
        _tag(_PTZ_NAMESPACE, "ProfileToken"),
    )
    profile.text = profile_token
    if preset_token:
        preset = ElementTree.SubElement(
            operation,
            _tag(_PTZ_NAMESPACE, "PresetToken"),
        )
        preset.text = preset_token
    return operation


def _absolute_move_operation(
    profile_token: str,
    *,
    pan: float,
    tilt: float,
    zoom: float,
) -> ElementTree.Element:
    operation = _operation("AbsoluteMove", profile_token=profile_token)
    position = ElementTree.SubElement(
        operation,
        _tag(_PTZ_NAMESPACE, "Position"),
    )
    ElementTree.SubElement(
        position,
        _tag(_SCHEMA_NAMESPACE, "PanTilt"),
        {"x": str(pan), "y": str(tilt)},
    )
    ElementTree.SubElement(
        position,
        _tag(_SCHEMA_NAMESPACE, "Zoom"),
        {"x": str(zoom)},
    )
    return operation


def _continuous_move_operation(
    profile_token: str,
    *,
    pan: float,
    tilt: float,
    zoom: float,
) -> ElementTree.Element:
    operation = _operation("ContinuousMove", profile_token=profile_token)
    velocity = ElementTree.SubElement(
        operation,
        _tag(_PTZ_NAMESPACE, "Velocity"),
    )
    if pan or tilt:
        ElementTree.SubElement(
            velocity,
            _tag(_SCHEMA_NAMESPACE, "PanTilt"),
            {"x": str(pan), "y": str(tilt)},
        )
    if zoom:
        ElementTree.SubElement(
            velocity,
            _tag(_SCHEMA_NAMESPACE, "Zoom"),
            {"x": str(zoom)},
        )
    timeout = ElementTree.SubElement(
        operation,
        _tag(_PTZ_NAMESPACE, "Timeout"),
    )
    timeout.text = "PT0.75S"
    return operation


def _stop_operation(profile_token: str) -> ElementTree.Element:
    operation = _operation("Stop", profile_token=profile_token)
    pan_tilt = ElementTree.SubElement(operation, _tag(_PTZ_NAMESPACE, "PanTilt"))
    pan_tilt.text = "true"
    zoom = ElementTree.SubElement(operation, _tag(_PTZ_NAMESPACE, "Zoom"))
    zoom.text = "true"
    return operation


def _set_preset_operation(
    profile_token: str,
    *,
    preset_name: str,
    preset_token: str,
) -> ElementTree.Element:
    operation = _operation("SetPreset", profile_token=profile_token)
    name = ElementTree.SubElement(operation, _tag(_PTZ_NAMESPACE, "PresetName"))
    name.text = preset_name
    token = ElementTree.SubElement(operation, _tag(_PTZ_NAMESPACE, "PresetToken"))
    token.text = preset_token
    return operation


def _soap_envelope(
    operation: ElementTree.Element,
    credentials: PtzCredentials | None,
) -> bytes:
    envelope = ElementTree.Element(_tag(_SOAP_NAMESPACE, "Envelope"))
    header = ElementTree.SubElement(
        envelope,
        _tag(_SOAP_NAMESPACE, "Header"),
    )
    if credentials is not None:
        _add_username_token(header, credentials)
    body = ElementTree.SubElement(
        envelope,
        _tag(_SOAP_NAMESPACE, "Body"),
    )
    body.append(operation)
    return ElementTree.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _add_username_token(
    header: ElementTree.Element,
    credentials: PtzCredentials,
) -> None:
    nonce = secrets.token_bytes(20)
    created = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    digest = hashlib.sha1(  # noqa: S324 - mandated by WS-Security UsernameToken 1.0
        nonce + created.encode("utf-8") + credentials.password.encode("utf-8")
    ).digest()
    security = ElementTree.SubElement(
        header,
        _tag(_WSSE_NAMESPACE, "Security"),
        {_tag(_SOAP_NAMESPACE, "mustUnderstand"): "true"},
    )
    token = ElementTree.SubElement(
        security,
        _tag(_WSSE_NAMESPACE, "UsernameToken"),
    )
    username = ElementTree.SubElement(
        token,
        _tag(_WSSE_NAMESPACE, "Username"),
    )
    username.text = credentials.username
    password = ElementTree.SubElement(
        token,
        _tag(_WSSE_NAMESPACE, "Password"),
        {"Type": _PASSWORD_DIGEST_TYPE},
    )
    password.text = base64.b64encode(digest).decode("ascii")
    nonce_element = ElementTree.SubElement(
        token,
        _tag(_WSSE_NAMESPACE, "Nonce"),
        {"EncodingType": _NONCE_ENCODING_TYPE},
    )
    nonce_element.text = base64.b64encode(nonce).decode("ascii")
    created_element = ElementTree.SubElement(
        token,
        _tag(_WSU_NAMESPACE, "Created"),
    )
    created_element.text = created


def _raise_for_response(response: OnvifHttpResponse) -> None:
    if response.status == 401:
        raise PtzExecutionError("ptz_authentication_failed")
    if response.status == 403:
        raise PtzExecutionError("ptz_authorization_failed")
    if not 200 <= response.status < 300:
        if _has_soap_fault(response.body):
            raise PtzExecutionError("ptz_onvif_fault")
        raise PtzExecutionError("ptz_onvif_http_error")
    if _has_soap_fault(response.body):
        raise PtzExecutionError("ptz_onvif_fault")


def _xml_root(payload: bytes) -> ElementTree.Element:
    if not payload:
        raise PtzExecutionError("ptz_onvif_response_invalid")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise PtzExecutionError("ptz_onvif_response_invalid") from error


def _has_soap_fault(payload: bytes) -> bool:
    try:
        root = _xml_root(payload)
    except PtzExecutionError:
        return False
    return root.find(f".//{{{_SOAP_NAMESPACE}}}Fault") is not None


def _movement_is_idle(payload: bytes) -> bool:
    root = _xml_root(payload)
    move_status = root.find(".//{*}MoveStatus")
    if move_status is None:
        raise PtzExecutionError("ptz_move_status_unavailable")
    values = [
        (child.text or "").strip().upper()
        for child in move_status
        if child.tag.rsplit("}", 1)[-1] in {"PanTilt", "Zoom"}
    ]
    if not values or any(value not in {"IDLE", "MOVING", "UNKNOWN"} for value in values):
        raise PtzExecutionError("ptz_move_status_invalid")
    return all(value == "IDLE" for value in values)


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"
