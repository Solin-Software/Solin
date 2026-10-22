"""Supervise the bundled LiveKit server process (hall or relay role).

The service writes the configuration (owner-only permissions), starts the binary, waits until
its HTTP port accepts connections, and restarts it with exponential backoff if it exits
unexpectedly. Too many restarts in a short window put it in the FAILED state, which the UI
surfaces instead of looping forever.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from .server_binary import ServerUnavailableError, server_executable
from .server_config import ServerSettings, render_config

log = logging.getLogger(__name__)

CONFIG_FILE_NAME = "livekit-server.json"
_STRIPPED_ENVIRONMENT = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_AUDIT",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
)


class ServerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"


class ConferenceServerService(QObject):
    """One supervised LiveKit server. Lives on the Qt thread that created it."""

    state_changed = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        config_dir: Path,
        *,
        executable_resolver: Callable[[], Path] = server_executable,
        ready_timeout_ms: int = 15000,
        probe_interval_ms: int = 250,
        max_restarts: int = 5,
        restart_window_s: float = 120.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_dir = Path(config_dir)
        self._resolve_executable = executable_resolver
        self._ready_timeout_ms = ready_timeout_ms
        self._max_restarts = max_restarts
        self._restart_window_s = restart_window_s
        self._settings: ServerSettings | None = None
        self._process: QProcess | None = None
        self._state = ServerState.STOPPED
        self._restart_delay_ms = 1000
        self._restarts: deque[float] = deque()
        self._output_tail: deque[str] = deque(maxlen=40)
        self._probe_socket: QTcpSocket | None = None
        self._probe = QTimer(self)
        self._probe.setInterval(probe_interval_ms)
        self._probe.timeout.connect(self._probe_ready)
        self._ready_deadline = QTimer(self)
        self._ready_deadline.setSingleShot(True)
        self._ready_deadline.timeout.connect(self._ready_timed_out)
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._launch)

    # -- public API ---------------------------------------------------------------------
    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def settings(self) -> ServerSettings | None:
        return self._settings

    @property
    def local_url(self) -> str:
        port = self._settings.http_port if self._settings else 0
        return f"ws://127.0.0.1:{port}"

    @property
    def config_path(self) -> Path:
        return self._config_dir / CONFIG_FILE_NAME

    def start(self, settings: ServerSettings) -> None:
        """Start (or restart with new settings) the server."""
        render_config(settings)  # validate before touching the running process
        self._settings = settings
        self._restarts.clear()
        self._restart_delay_ms = 1000
        self._stop_process()
        self._launch()

    def stop(self, *, wait: bool = False, timeout_ms: int = 3000) -> None:
        self._restart_timer.stop()
        process = self._process
        self._stop_process()
        if wait and process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.waitForFinished(timeout_ms)
        self._set_state(ServerState.STOPPED)

    # -- process lifecycle --------------------------------------------------------------
    def _launch(self) -> None:
        settings = self._settings
        if settings is None:
            return
        try:
            executable = self._resolve_executable()
            self._write_config(settings)
        except (ServerUnavailableError, OSError) as exc:
            log.error("Cannot start the conference server: %s", exc)
            self._set_state(ServerState.FAILED)
            self.error.emit(str(exc))
            return
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProcessEnvironment(self._environment())
        process.readyReadStandardOutput.connect(lambda: self._read_output(process))
        process.finished.connect(lambda *_: self._exited(process))
        process.errorOccurred.connect(lambda _: self._process_error(process))
        self._process = process
        self._output_tail.clear()
        self._set_state(ServerState.STARTING)
        process.start(str(executable), ["--config", str(self.config_path)])
        self._ready_deadline.start(self._ready_timeout_ms)
        self._probe.start()

    def _write_config(self, settings: ServerSettings) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_path
        data = render_config(settings).encode("utf-8")
        # Owner-only: the file holds the API secret.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        if sys.platform != "win32":
            os.chmod(path, 0o600)

    @staticmethod
    def _environment() -> QProcessEnvironment:
        environment = QProcessEnvironment.systemEnvironment()
        for name in _STRIPPED_ENVIRONMENT:
            environment.remove(name)
        return environment

    def _stop_process(self) -> None:
        self._probe.stop()
        self._ready_deadline.stop()
        self._drop_probe_socket()
        process, self._process = self._process, None
        if process is None:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            process.deleteLater()
            return
        process.terminate()
        if not process.waitForFinished(2000):
            process.kill()
            process.waitForFinished(1000)
        process.deleteLater()

    # -- readiness ----------------------------------------------------------------------
    def _probe_ready(self) -> None:
        if self._process is None or self._settings is None or self._probe_socket is not None:
            return
        socket = QTcpSocket(self)
        self._probe_socket = socket
        socket.connected.connect(lambda: self._probe_succeeded(socket))
        socket.errorOccurred.connect(lambda _: self._probe_failed(socket))
        socket.connectToHost("127.0.0.1", self._settings.http_port)

    def _probe_succeeded(self, socket: QTcpSocket) -> None:
        if socket is not self._probe_socket:
            return
        self._drop_probe_socket()
        self._probe.stop()
        self._ready_deadline.stop()
        self._restart_delay_ms = 1000
        self._set_state(ServerState.RUNNING)

    def _probe_failed(self, socket: QTcpSocket) -> None:
        if socket is self._probe_socket:
            self._drop_probe_socket()

    def _drop_probe_socket(self) -> None:
        socket, self._probe_socket = self._probe_socket, None
        if socket is not None:
            if socket.state() != QAbstractSocket.SocketState.UnconnectedState:
                socket.abort()
            socket.deleteLater()

    def _ready_timed_out(self) -> None:
        log.warning("Conference server did not become ready in %d ms", self._ready_timeout_ms)
        self._recover("not ready in time")

    # -- failures -----------------------------------------------------------------------
    def _read_output(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput().data()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self._output_tail.append(line)
                log.debug("livekit-server: %s", line)

    def _exited(self, process: QProcess) -> None:
        if process is self._process:
            code = process.exitCode()
            self._process = None
            process.deleteLater()
            self._recover(f"exited with code {code}")
        else:
            process.deleteLater()

    def _process_error(self, process: QProcess) -> None:
        if process is self._process and process.error() == QProcess.ProcessError.FailedToStart:
            self._process = None
            process.deleteLater()
            self._recover("failed to start")

    def _recover(self, reason: str) -> None:
        tail = " | ".join(list(self._output_tail)[-3:])
        log.warning("Conference server %s%s", reason, f": {tail}" if tail else "")
        self._stop_process()
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > self._restart_window_s:
            self._restarts.popleft()
        if len(self._restarts) >= self._max_restarts:
            self._set_state(ServerState.FAILED)
            self.error.emit(f"Conference server keeps failing ({reason})")
            return
        self._restarts.append(now)
        self._set_state(ServerState.RESTARTING)
        self._restart_timer.start(self._restart_delay_ms)
        self._restart_delay_ms = min(30000, self._restart_delay_ms * 2)

    def _set_state(self, state: ServerState) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state.value)
