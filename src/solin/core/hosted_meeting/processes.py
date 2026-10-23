"""Supervised helper processes (Galene, the bridge, frpc).

``SupervisedProcess`` starts a command, forwards its output to the log, waits for it
to become ready, and restarts it with backoff if it exits while it should be running.
It is Qt-free; owners observe state through the ``on_state`` callback, which runs on a
supervisor thread.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from solin.core.network import http

log = logging.getLogger(__name__)


class ProcessState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"


ReadyCheck = Callable[[], bool]


def free_tcp_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def http_ready(url: str, *, timeout: float = 1.0) -> ReadyCheck:
    def check() -> bool:
        try:
            http.get(url, timeout=timeout)
            return True
        except http.HttpStatusError as error:
            return error.status_code < 500
        except http.HttpError:
            return False

    return check


class SupervisedProcess:
    def __init__(
        self,
        name: str,
        command: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        ready: ReadyCheck | None = None,
        ready_timeout: float = 10.0,
        max_restarts: int = 5,
        on_state: Callable[[ProcessState, str], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self._command = [str(part) for part in command]
        self._cwd = cwd
        self._env = env
        self._ready = ready
        self._ready_timeout = ready_timeout
        self._max_restarts = max_restarts
        self._on_state = on_state
        self._on_line = on_line
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = ProcessState.STOPPED
        self._ready_event = threading.Event()

    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    def _set_state(self, state: ProcessState, detail: str = "") -> None:
        self._state = state
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:  # noqa: BLE001 - observer boundary
                log.exception("%s state observer failed", self.name)

    def start(self, *, wait: bool = True) -> bool:
        """Start supervising; with ``wait``, block until ready (True) or failed (False)."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._state is ProcessState.RUNNING
            self._stop.clear()
            self._ready_event.clear()
            self._thread = threading.Thread(target=self._supervise, name=f"solin-{self.name}", daemon=True)
            self._thread.start()
        if not wait:
            return True
        self._ready_event.wait(self._ready_timeout + 2.0)
        return self._state is ProcessState.RUNNING

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._terminate(timeout)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        self._set_state(ProcessState.STOPPED)

    def _terminate(self, timeout: float) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(2.0)
        except OSError:
            pass

    def _launch(self) -> subprocess.Popen[bytes]:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        return subprocess.Popen(  # noqa: S603 - fixed helper command
            self._command,
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )

    def _pump(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            log.debug("[%s] %s", self.name, line)
            if self._on_line is not None:
                try:
                    self._on_line(line)
                except Exception:  # noqa: BLE001 - observer boundary
                    log.exception("%s line observer failed", self.name)

    def _wait_ready(self, process: subprocess.Popen[bytes]) -> bool:
        if self._ready is None:
            return process.poll() is None
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if process.poll() is not None:
                return False
            if self._ready():
                return True
            time.sleep(0.1)
        return False

    def _supervise(self) -> None:
        restarts = 0
        self._set_state(ProcessState.STARTING)
        while not self._stop.is_set():
            try:
                process = self._launch()
            except OSError as error:
                log.error("could not start %s: %s", self.name, error)
                self._set_state(ProcessState.FAILED, str(error))
                self._ready_event.set()
                return
            self._process = process
            threading.Thread(target=self._pump, args=(process,), name=f"solin-{self.name}-log", daemon=True).start()
            if self._wait_ready(process):
                self._set_state(ProcessState.RUNNING)
                self._ready_event.set()
                began = time.monotonic()
                process.wait()
                if time.monotonic() - began > 60.0:
                    restarts = 0  # a long healthy run resets the restart budget
            else:
                self._terminate(2.0)
            if self._stop.is_set():
                return
            restarts += 1
            if restarts > self._max_restarts:
                log.error("%s exited %d times; giving up", self.name, restarts)
                self._set_state(ProcessState.FAILED, f"exited with code {process.returncode}")
                self._ready_event.set()
                return
            delay = min(10.0, 0.5 * 2 ** (restarts - 1))
            log.warning("%s exited (code %s); restarting in %.1f s", self.name, process.returncode, delay)
            self._set_state(ProcessState.RESTARTING, f"exited with code {process.returncode}")
            if self._stop.wait(delay):
                return
