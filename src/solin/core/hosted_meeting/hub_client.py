"""The meeting server's API, as Solin uses it (Qt-free, blocking: call it off the UI thread).

The server (the hub; the solin-hub repository) creates meeting codes. Solin asks for one
with the sha256 of a fresh secret, and keeps the secret: it proves ownership of the code
from then on (start time and active/retired state, the meeting's tunnel, TURN
credentials). The server is open to every Solin; there is no account.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from solin.core.network.http import HttpError, request_json

from .settings import HubServer

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
MAX_SYNC = 300   # the server's limit per request


class HubError(Exception):
    """The server could not be reached or refused the request; the message is for the operator."""


@dataclass(frozen=True, slots=True)
class TunnelInfo:
    frp_port: int
    frp_token: str


@dataclass(frozen=True, slots=True)
class TurnCredentials:
    urls: tuple[str, ...]
    username: str
    credential: str


@dataclass(frozen=True, slots=True)
class CodeState:
    code: str
    secret: str
    starts_at: str      # ISO, or "" (no countdown)
    active: bool        # False: the link says "no longer valid"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def owner_of(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class HubClient:
    def __init__(self, server: HubServer, *, timeout: float = TIMEOUT_S) -> None:
        self.server = server
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = request_json(method, self.server.public_origin + path, body=body, timeout=self.timeout)
        except HttpError as error:
            log.info("meeting server %s: %s", self.server.domain, error)
            raise HubError(f"{self.server.domain} cannot be reached") from error
        try:
            data = response.json() if response.content else {}
        except HttpError:
            data = {}
        if response.status_code >= 400:
            detail = str(data.get("error") or "") if isinstance(data, dict) else ""
            raise HubError(f"{self.server.domain} answered {response.status_code}" + (f": {detail}" if detail else ""))
        return data

    def create_code(self, secret: str, starts_at: str = "") -> str:
        """A new code, owned by ``secret``."""

        data = self._call("POST", "/api/codes", {"owner": owner_of(secret), "startsAt": starts_at})
        code = str(data.get("code") or "") if isinstance(data, dict) else ""
        if not code:
            raise HubError(f"{self.server.domain} gave no code")
        return code

    def sync_codes(self, codes: list[CodeState]) -> set[str]:
        """Tell the server each code's start time and state; returns the codes it does not know as ours."""

        rejected: set[str] = set()
        for start in range(0, len(codes), MAX_SYNC):
            batch = codes[start:start + MAX_SYNC]
            data = self._call("POST", "/api/codes/sync", {"codes": [
                {"code": c.code, "secret": c.secret, "startsAt": c.starts_at, "active": c.active} for c in batch]})
            rejected |= {str(code) for code in (data.get("rejected") or [])} if isinstance(data, dict) else set()
        return rejected

    def tunnel_info(self) -> TunnelInfo:
        data = self._call("GET", "/api/hub")
        try:
            return TunnelInfo(frp_port=int(data["frpPort"]), frp_token=str(data["frpToken"]))
        except (KeyError, TypeError, ValueError) as error:
            raise HubError(f"{self.server.domain} gave no tunnel settings") from error

    def turn_credentials(self, code: str, secret: str) -> TurnCredentials | None:
        """TURN credentials for this meeting, or None when the server has no TURN relay."""

        try:
            data = self._call("POST", "/api/turn", {"code": code, "secret": secret})
        except HubError as error:
            if "answered 404" in str(error):
                return None
            raise
        try:
            return TurnCredentials(urls=tuple(str(u) for u in data["urls"]), username=str(data["username"]),
                                   credential=str(data["credential"]))
        except (KeyError, TypeError) as error:
            raise HubError(f"{self.server.domain} gave no TURN credentials") from error
