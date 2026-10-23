"""Run Galene for the hall: configuration on disk plus a supervised process."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .binaries import Helper, find_helper
from .galene_config import GaleneLayout, TurnServer
from .processes import ProcessState, SupervisedProcess, free_tcp_port, http_ready

log = logging.getLogger(__name__)


class GaleneUnavailable(RuntimeError):
    pass


class GaleneServer:
    def __init__(
        self,
        root: Path,
        *,
        binary: Path | None = None,
        port: int | None = None,
        on_state: Callable[[ProcessState, str], None] | None = None,
    ) -> None:
        self.layout = GaleneLayout(root)
        self._binary = binary
        self.port = port or free_tcp_port()
        self._on_state = on_state
        self._process: SupervisedProcess | None = None

    @property
    def state(self) -> ProcessState:
        return self._process.state if self._process is not None else ProcessState.STOPPED

    def start(self, *, turn: TurnServer | None = None) -> None:
        binary = self._binary or find_helper(Helper.GALENE)
        if binary is None:
            raise GaleneUnavailable("the Galene media server is not installed")
        self.layout.prepare()
        self.layout.write_server_config([])
        self.layout.write_ice_servers(turn)
        command = [
            binary,
            "-insecure",
            "-http", f"127.0.0.1:{self.port}",
            "-turn", "",
            "-data", self.layout.data,
            "-groups", self.layout.groups,
            "-static", self.layout.static,
        ]
        self._process = SupervisedProcess(
            "galene", command, cwd=self.layout.root,
            ready=http_ready(f"http://127.0.0.1:{self.port}/"), ready_timeout=15.0,
            on_state=self._on_state,
        )
        if not self._process.start(wait=True):
            raise GaleneUnavailable("the Galene media server did not start")
        log.info("Galene listening on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._process is not None:
            self._process.stop()
            self._process = None
