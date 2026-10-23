"""The hall's tunnel to the hub: a supervised ``frpc``.

The hall dials *out* to the hub's frps (so it works behind CGNAT) and exposes one raw
TCP service: the meeting web server. The hub serves it at
``https://<hall>.<hub-domain>`` (see ``deploy/hub/``). The configuration follows
``deploy/hub/hall/frpc.toml.template``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from .processes import ProcessState, SupervisedProcess
from .settings import HubConnection

log = logging.getLogger(__name__)

_ONLINE = re.compile(r"start proxy success|\[[^\]]+\] start proxy success", re.IGNORECASE)
_LOGIN_OK = re.compile(r"login to server success", re.IGNORECASE)
_FAILED = re.compile(r"login to the server failed|login to server failed|start error|"
                     r"proxy \[[^\]]+\] start error|authorization failed", re.IGNORECASE)


class TunnelState(StrEnum):
    OFF = "off"
    CONNECTING = "connecting"
    ONLINE = "online"
    FAILED = "failed"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def frpc_config(hub: HubConnection, local_port: int) -> str:
    return "\n".join([
        f"serverAddr = {_toml_string(hub.frp_addr)}",
        f"serverPort = {int(hub.frp_port)}",
        'auth.method = "token"',
        f"auth.token = {_toml_string(hub.frp_token)}",
        "loginFailExit = false",
        "transport.tcpMux = true",
        'transport.protocol = "tcp"',
        'log.to = "console"',
        'log.level = "info"',
        f"metadatas.hall = {_toml_string(hub.hall)}",
        f"metadatas.hall_key = {_toml_string(hub.hall_key)}",
        "",
        "[[proxies]]",
        f"name = {_toml_string(hub.hall)}",
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
        hub: HubConnection,
        *,
        local_port: int,
        on_state: Callable[[TunnelState, str], None] | None = None,
    ) -> None:
        self._binary = binary
        self._root = Path(root)
        self._hub = hub
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
        config.write_text(frpc_config(self._hub, self._local_port), encoding="utf-8")
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
