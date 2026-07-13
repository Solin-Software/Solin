"""Safe X11 focus management for Linux Zoom shortcut automation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess

log = logging.getLogger(__name__)

_ZOOM_EXECUTABLE_NAMES = frozenset({"zoom", "zoom.real"})
_XDOTOOL_QUERY_TIMEOUT_SECONDS = 1.0
_XDOTOOL_ACTIVATION_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class LinuxZoomWindow:
    window_id: str
    pid: int
    owner: str
    x: int
    y: int
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class _WindowIdentity:
    window_id: str
    pid: int


@dataclass(frozen=True)
class LinuxZoomFocusSession:
    xdotool: str
    zoom_window: _WindowIdentity
    previous_window: _WindowIdentity | None


def list_zoom_windows() -> dict[str, LinuxZoomWindow] | None:
    """Return visible X11 windows owned by an exact Zoom executable."""
    if os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland":
        return None

    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None

    processes = _linux_zoom_processes()
    if not processes:
        return {}

    env = os.environ.copy()
    windows: dict[str, LinuxZoomWindow] = {}
    for pid, owner in processes.items():
        result = _run_xdotool(
            xdotool,
            "search",
            "--onlyvisible",
            "--pid",
            str(pid),
            env=env,
        )
        if result is None or result.returncode not in (0, 1):
            continue

        for raw_window_id in result.stdout.splitlines():
            window_id = raw_window_id.strip()
            if not window_id.isdecimal():
                continue
            if _window_pid(xdotool, window_id, env) != pid:
                continue
            geometry = _window_geometry(xdotool, window_id, env)
            if geometry is None:
                continue
            x, y, width, height = geometry
            if width <= 0 or height <= 0:
                continue
            windows[window_id] = LinuxZoomWindow(
                window_id=window_id,
                pid=pid,
                owner=owner,
                x=x,
                y=y,
                width=width,
                height=height,
            )

    return windows


def acquire_zoom_focus() -> LinuxZoomFocusSession | None:
    """Focus the safest visible Zoom window and verify X11 accepted the request."""
    windows = list_zoom_windows()
    if windows is None:
        log.warning("Zoom focus automation is unavailable on this Linux session")
        return None
    if not windows:
        log.warning("No visible Zoom window was found for shortcut automation")
        return None

    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None
    env = os.environ.copy()
    active_window = _active_window_identity(xdotool, env)
    active_zoom_window = (
        windows.get(active_window.window_id)
        if active_window is not None else None
    )
    if (
        active_window is not None
        and active_zoom_window is not None
        and active_zoom_window.pid == active_window.pid
    ):
        return LinuxZoomFocusSession(
            xdotool=xdotool,
            zoom_window=_WindowIdentity(
                active_zoom_window.window_id,
                active_zoom_window.pid,
            ),
            previous_window=None,
        )

    target = max(
        windows.values(),
        key=lambda window: (window.area, window.width, window.height),
    )
    session = LinuxZoomFocusSession(
        xdotool=xdotool,
        zoom_window=_WindowIdentity(target.window_id, target.pid),
        previous_window=active_window,
    )
    result = _run_xdotool(
        xdotool,
        "windowactivate",
        "--sync",
        target.window_id,
        env=env,
        timeout=_XDOTOOL_ACTIVATION_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        log.warning("Could not activate Zoom window %s", target.window_id)
        restore_previous_focus(session)
        return None
    if not zoom_has_focus(session):
        log.warning("Zoom window %s did not receive keyboard focus", target.window_id)
        restore_previous_focus(session)
        return None
    return session


def zoom_has_focus(session: LinuxZoomFocusSession) -> bool:
    """Confirm the active X11 window still has the expected Zoom PID."""
    active = _active_window_identity(session.xdotool, os.environ.copy())
    return active == session.zoom_window


def restore_previous_focus(session: LinuxZoomFocusSession) -> None:
    """Restore the prior window only while its X11 id still belongs to its PID."""
    previous = session.previous_window
    if previous is None:
        return

    env = os.environ.copy()
    if _window_pid(session.xdotool, previous.window_id, env) != previous.pid:
        log.debug("Skipping focus restore because the previous window no longer exists")
        return

    result = _run_xdotool(
        session.xdotool,
        "windowactivate",
        "--sync",
        previous.window_id,
        env=env,
        timeout=_XDOTOOL_ACTIVATION_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        log.debug("Could not restore focus to X11 window %s", previous.window_id)


def _linux_zoom_processes(proc_root: Path = Path("/proc")) -> dict[int, str]:
    processes: dict[int, str] = {}
    if not proc_root.is_dir():
        return processes

    for proc_dir in proc_root.iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            comm = (proc_dir / "comm").read_text(
                encoding="utf-8",
                errors="ignore",
            ).strip()
            raw_cmdline = (proc_dir / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue

        argv0 = os.fsdecode(raw_cmdline[0]) if raw_cmdline and raw_cmdline[0] else ""
        executable_names = {
            Path(value).name.casefold()
            for value in (comm, argv0, _process_executable(proc_dir))
            if value
        }
        if executable_names.isdisjoint(_ZOOM_EXECUTABLE_NAMES):
            continue
        processes[int(proc_dir.name)] = comm or Path(argv0).name

    return processes


def _process_executable(proc_dir: Path) -> str:
    try:
        return os.readlink(proc_dir / "exe")
    except OSError:
        return ""


def _active_window_identity(
    xdotool: str,
    env: dict[str, str],
) -> _WindowIdentity | None:
    result = _run_xdotool(xdotool, "getactivewindow", env=env)
    if result is None or result.returncode != 0:
        return None
    window_id = result.stdout.strip()
    if not window_id.isdecimal():
        return None
    pid = _window_pid(xdotool, window_id, env)
    if pid is None:
        return None
    return _WindowIdentity(window_id, pid)


def _window_pid(xdotool: str, window_id: str, env: dict[str, str]) -> int | None:
    result = _run_xdotool(xdotool, "getwindowpid", window_id, env=env)
    if result is None or result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def _window_geometry(
    xdotool: str,
    window_id: str,
    env: dict[str, str],
) -> tuple[int, int, int, int] | None:
    result = _run_xdotool(
        xdotool,
        "getwindowgeometry",
        "--shell",
        window_id,
        env=env,
    )
    if result is None or result.returncode != 0:
        return None

    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, separator, raw_value = line.partition("=")
        if separator and key in {"X", "Y", "WIDTH", "HEIGHT"}:
            try:
                values[key] = int(raw_value)
            except ValueError:
                return None
    try:
        return values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]
    except KeyError:
        return None


def _run_xdotool(
    xdotool: str,
    *arguments: str,
    env: dict[str, str],
    timeout: float = _XDOTOOL_QUERY_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [xdotool, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("xdotool %s failed: %s", arguments[0], exc)
        return None
