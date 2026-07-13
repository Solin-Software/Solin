from __future__ import annotations

from pathlib import Path
import subprocess

from solin.core.integrations.automation import linux_zoom_focus


def _completed(*arguments: str, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(arguments, returncode, stdout, "")


def _window(
    window_id: str,
    *,
    pid: int,
    width: int,
    height: int,
    x: int = 0,
    y: int = 0,
) -> linux_zoom_focus.LinuxZoomWindow:
    return linux_zoom_focus.LinuxZoomWindow(
        window_id=window_id,
        pid=pid,
        owner="zoom",
        x=x,
        y=y,
        width=width,
        height=height,
    )


def _write_process(
    root: Path,
    pid: int,
    *,
    comm: str,
    argv0: str,
) -> None:
    process = root / str(pid)
    process.mkdir()
    (process / "comm").write_text(comm, encoding="utf-8")
    (process / "cmdline").write_bytes(argv0.encode() + b"\0--flag\0")


def test_linux_zoom_processes_require_an_exact_executable_name(tmp_path):
    _write_process(tmp_path, 100, comm="zoom", argv0="/opt/zoom/zoom")
    _write_process(tmp_path, 200, comm="python", argv0="/tmp/zoom-helper/run.py")
    _write_process(tmp_path, 300, comm="launcher", argv0="/opt/zoom/zoom.real")

    assert linux_zoom_focus._linux_zoom_processes(tmp_path) == {
        100: "zoom",
        300: "launcher",
    }


def test_list_zoom_windows_verifies_pid_and_geometry(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(linux_zoom_focus.shutil, "which", lambda _name: "/bin/xdotool")
    monkeypatch.setattr(
        linux_zoom_focus,
        "_linux_zoom_processes",
        lambda: {42: "zoom"},
    )

    def _run(_xdotool, *arguments, **_kwargs):
        if arguments[0] == "search":
            return _completed(*arguments, stdout="100\n200\n")
        if arguments[:2] == ("getwindowpid", "100"):
            return _completed(*arguments, stdout="42\n")
        if arguments[:2] == ("getwindowpid", "200"):
            return _completed(*arguments, stdout="99\n")
        if arguments[0] == "getwindowgeometry":
            return _completed(
                *arguments,
                stdout="X=10\nY=20\nWIDTH=1280\nHEIGHT=720\n",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(linux_zoom_focus, "_run_xdotool", _run)

    assert linux_zoom_focus.list_zoom_windows() == {
        "100": _window("100", pid=42, width=1280, height=720, x=10, y=20)
    }


def test_list_zoom_windows_rejects_native_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(
        linux_zoom_focus.shutil,
        "which",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not query xdotool")),
    )

    assert linux_zoom_focus.list_zoom_windows() is None


def test_acquire_zoom_focus_activates_largest_window_and_verifies_owner(monkeypatch):
    windows = {
        "100": _window("100", pid=42, width=320, height=80),
        "200": _window("200", pid=42, width=1280, height=720),
    }
    active = iter(
        [
            linux_zoom_focus._WindowIdentity("10", 7),
            linux_zoom_focus._WindowIdentity("200", 42),
        ]
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(linux_zoom_focus, "list_zoom_windows", lambda: windows)
    monkeypatch.setattr(linux_zoom_focus.shutil, "which", lambda _name: "/bin/xdotool")
    monkeypatch.setattr(
        linux_zoom_focus,
        "_active_window_identity",
        lambda _xdotool, _env: next(active),
    )
    monkeypatch.setattr(
        linux_zoom_focus,
        "_run_xdotool",
        lambda _xdotool, *arguments, **_kwargs: (
            calls.append(arguments) or _completed(*arguments)
        ),
    )

    session = linux_zoom_focus.acquire_zoom_focus()

    assert session == linux_zoom_focus.LinuxZoomFocusSession(
        xdotool="/bin/xdotool",
        zoom_window=linux_zoom_focus._WindowIdentity("200", 42),
        previous_window=linux_zoom_focus._WindowIdentity("10", 7),
    )
    assert calls == [("windowactivate", "--sync", "200")]


def test_acquire_zoom_focus_restores_previous_window_when_verification_fails(
    monkeypatch,
):
    windows = {"200": _window("200", pid=42, width=1280, height=720)}
    active = iter(
        [
            linux_zoom_focus._WindowIdentity("10", 7),
            linux_zoom_focus._WindowIdentity("300", 99),
        ]
    )
    activations: list[tuple[str, ...]] = []
    monkeypatch.setattr(linux_zoom_focus, "list_zoom_windows", lambda: windows)
    monkeypatch.setattr(linux_zoom_focus.shutil, "which", lambda _name: "/bin/xdotool")
    monkeypatch.setattr(
        linux_zoom_focus,
        "_active_window_identity",
        lambda _xdotool, _env: next(active),
    )
    monkeypatch.setattr(linux_zoom_focus, "_window_pid", lambda *_args: 7)
    monkeypatch.setattr(
        linux_zoom_focus,
        "_run_xdotool",
        lambda _xdotool, *arguments, **_kwargs: (
            activations.append(arguments) or _completed(*arguments)
        ),
    )

    assert linux_zoom_focus.acquire_zoom_focus() is None
    assert activations == [
        ("windowactivate", "--sync", "200"),
        ("windowactivate", "--sync", "10"),
    ]


def test_restore_previous_focus_rejects_reused_window_id(monkeypatch):
    session = linux_zoom_focus.LinuxZoomFocusSession(
        xdotool="/bin/xdotool",
        zoom_window=linux_zoom_focus._WindowIdentity("200", 42),
        previous_window=linux_zoom_focus._WindowIdentity("10", 7),
    )
    activations: list[tuple[str, ...]] = []
    monkeypatch.setattr(linux_zoom_focus, "_window_pid", lambda *_args: 999)
    monkeypatch.setattr(
        linux_zoom_focus,
        "_run_xdotool",
        lambda _xdotool, *arguments, **_kwargs: (
            activations.append(arguments) or _completed(*arguments)
        ),
    )

    linux_zoom_focus.restore_previous_focus(session)

    assert activations == []


def test_restore_previous_focus_activates_the_same_process_window(monkeypatch):
    session = linux_zoom_focus.LinuxZoomFocusSession(
        xdotool="/bin/xdotool",
        zoom_window=linux_zoom_focus._WindowIdentity("200", 42),
        previous_window=linux_zoom_focus._WindowIdentity("10", 7),
    )
    activations: list[tuple[str, ...]] = []
    monkeypatch.setattr(linux_zoom_focus, "_window_pid", lambda *_args: 7)
    monkeypatch.setattr(
        linux_zoom_focus,
        "_run_xdotool",
        lambda _xdotool, *arguments, **_kwargs: (
            activations.append(arguments) or _completed(*arguments)
        ),
    )

    linux_zoom_focus.restore_previous_focus(session)

    assert activations == [("windowactivate", "--sync", "10")]
