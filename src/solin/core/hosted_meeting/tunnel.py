"""The hall's tunnel to the hub: a supervised ``frpc``.

The hall dials *out* to the hub's frps (so it works behind CGNAT) and exposes one raw
TCP service: the meeting web server, named after the meeting code and authorized by the
code's secret. The hub serves it at ``https://<hub-domain>/<code>`` (:mod:`.hub_client`;
the hub lives in the solin-hub repository,
https://github.com/Solin-Software/solin-hub). The configuration follows that
repository's ``client/frpc.toml.template``.

The tunnel is TLS. For the official server, frpc accepts only the server's own
certificate (:data:`OFFICIAL_TUNNEL_CERT`, pinned here), so it hands the tunnel token
and the code's secret to that server and nothing else.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from .hub_client import TunnelInfo
from .processes import ProcessState, SupervisedProcess
from .settings import DEFAULT_SERVER, HubServer

log = logging.getLogger(__name__)

_ONLINE = re.compile(r"start proxy success|\[[^\]]+\] start proxy success", re.IGNORECASE)
_LOGIN_OK = re.compile(r"login to server success", re.IGNORECASE)
_FAILED = re.compile(r"login to the server failed|login to server failed|start error|"
                     r"proxy \[[^\]]+\] start error|authorization failed", re.IGNORECASE)


# The official server's tunnel certificate (solin-hub: tls/tunnel.crt, made by
# scripts/tunnel_cert.sh; valid until 2046). Replacing it on the server needs a Solin release.
OFFICIAL_TUNNEL_CERT = """\
-----BEGIN CERTIFICATE-----
MIIBxTCCAWygAwIBAgIUD57leg+DeonENglbTPCpFWYUv3kwCgYIKoZIzj0EAwIw
GTEXMBUGA1UEAwwObWVldC5zb2xpbi5ydW4wHhcNMjYxMTE5MDI0NjQzWhcNNDYx
MTE0MDI0NjQzWjAZMRcwFQYDVQQDDA5tZWV0LnNvbGluLnJ1bjBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABGulLwNsdGN7Ef57XCAEn9KzNtY3P9LrHHz7DPn5lyv2
SUVxCeTTwwQhTMOaEcb9whiAJeq6VfGORz624qtacbGjgZEwgY4wHQYDVR0OBBYE
FD/Q+ekU/mnSxOQZAv1036x459ZcMB8GA1UdIwQYMBaAFD/Q+ekU/mnSxOQZAv10
36x459ZcMBkGA1UdEQQSMBCCDm1lZXQuc29saW4ucnVuMAwGA1UdEwEB/wQCMAAw
DgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUFBwMBMAoGCCqGSM49BAMC
A0cAMEQCIFJXo/Y36k36isINsrF3ohp8qmeJH+E69Tp25fyxPT2vAiB7HI09Mm7C
2gOm9Gxlg1DKfrlFGU9a2nIF+Dl1/X6QZw==
-----END CERTIFICATE-----
"""
# A development server's tunnel certificate (a PEM file), for SOLIN_MEETING_SERVER.
CERT_OVERRIDE = "SOLIN_MEETING_SERVER_CERT"


def tunnel_certificate(hub: HubServer) -> str | None:
    """The certificate frpc must see, or None (a development server without one: not verified)."""

    if hub.domain == DEFAULT_SERVER:
        return OFFICIAL_TUNNEL_CERT
    path = os.environ.get(CERT_OVERRIDE, "").strip()
    try:
        return Path(path).read_text(encoding="utf-8") if path else None
    except OSError as error:
        log.warning("%s: cannot read %s (%s)", CERT_OVERRIDE, path, error)
        return None


class TunnelState(StrEnum):
    OFF = "off"
    CONNECTING = "connecting"
    ONLINE = "online"
    FAILED = "failed"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def frpc_config(hub: HubServer, tunnel: TunnelInfo, code: str, secret: str, local_port: int,
                ca_file: Path | None = None) -> str:
    """``ca_file``: the only certificate the server may present; None = TLS without checking it."""

    host = hub.domain.split(":", 1)[0]
    tls = ["transport.tls.enable = true", f"transport.tls.serverName = {_toml_string(host)}"]
    if ca_file is not None:
        tls.append(f"transport.tls.trustedCaFile = {_toml_string(str(ca_file))}")
    return "\n".join([
        f"serverAddr = {_toml_string(host)}",
        f"serverPort = {int(tunnel.frp_port)}",
        'auth.method = "token"',
        f"auth.token = {_toml_string(tunnel.frp_token)}",
        "loginFailExit = false",
        "transport.tcpMux = true",
        'transport.protocol = "tcp"',
        *tls,
        'log.to = "console"',
        'log.level = "info"',
        f"metadatas.code = {_toml_string(code)}",
        f"metadatas.secret = {_toml_string(secret)}",
        "",
        "[[proxies]]",
        f"name = {_toml_string(code)}",
        'type = "tcp"',
        'localIP = "127.0.0.1"',
        f"localPort = {int(local_port)}",
        "remotePort = 0",
        "",
    ])


class FrpTunnel:
    def __init__(
        self,
        binary: Path,
        root: Path,
        hub: HubServer,
        tunnel: TunnelInfo,
        *,
        code: str,
        secret: str,
        local_port: int,
        on_state: Callable[[TunnelState, str], None] | None = None,
    ) -> None:
        self._binary = binary
        self._root = Path(root)
        self._hub = hub
        self._tunnel = tunnel
        self._code = code
        self._secret = secret
        self._local_port = local_port
        self._on_state = on_state
        self._process: SupervisedProcess | None = None
        self.state = TunnelState.OFF

    def _set(self, state: TunnelState, detail: str = "") -> None:
        if state is self.state:
            return
        self.state = state
        if self._on_state is not None:
            self._on_state(state, detail)

    def _on_line(self, line: str) -> None:
        if _ONLINE.search(line):
            self._set(TunnelState.ONLINE)
        elif _FAILED.search(line):
            log.warning("hub tunnel: %s", line)
            self._set(TunnelState.CONNECTING, line)  # frpc keeps retrying (loginFailExit = false)

    def _on_process(self, state: ProcessState, detail: str) -> None:
        if state in (ProcessState.RESTARTING, ProcessState.STARTING):
            self._set(TunnelState.CONNECTING, detail)
        elif state is ProcessState.FAILED:
            self._set(TunnelState.FAILED, detail)

    def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        config = self._root / "frpc.toml"
        certificate = tunnel_certificate(self._hub)
        ca_file = self._root / "hub-tunnel.crt" if certificate else None
        if ca_file is not None:
            ca_file.write_text(certificate, encoding="utf-8")
        else:
            log.warning("hub tunnel: %s has no known certificate; the connection is not verified", self._hub.domain)
        config.write_text(frpc_config(self._hub, self._tunnel, self._code, self._secret, self._local_port, ca_file),
                          encoding="utf-8")
        try:
            config.chmod(0o600)
        except OSError:
            pass
        self._set(TunnelState.CONNECTING)
        self._process = SupervisedProcess("frpc", [self._binary, "-c", config], max_restarts=1000,
                                          on_state=self._on_process, on_line=self._on_line)
        self._process.start(wait=False)

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.stop()
        (self._root / "frpc.toml").unlink(missing_ok=True)
        self._set(TunnelState.OFF)
