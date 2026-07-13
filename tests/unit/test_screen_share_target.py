from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from solin.core.integrations.automation import screen_share


def _window(
    window_id: str,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
) -> screen_share._ZoomWindowInfo:
    return screen_share._ZoomWindowInfo(
        window_id=window_id,
        owner="zoom.exe",
        x=x,
        y=y,
        width=width,
        height=height,
    )


def test_execute_start_share_clicks_absolute_configured_position(monkeypatch):
    dialog = _window("dialog", x=-1200, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: {})
    monkeypatch.setattr(
        screen_share,
        "_wait_for_new_zoom_window",
        lambda _initial, _monitor=None: dialog,
    )
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share.execute_start_share("Alt+S", 300, 250) is True
    assert clicks == [(300, 250, 2, 120)]


def test_execute_start_share_warns_once_when_mouse_moves_during_wait(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    calls = {"windows": 0}
    positions = iter([(10, 10), (10, 10), (140, 10), (140, 10), (140, 10)])
    warnings: list[bool] = []

    def _list_windows():
        calls["windows"] += 1
        return {"dialog": dialog} if calls["windows"] >= 2 else {}

    monkeypatch.setattr(screen_share, "_list_zoom_windows", _list_windows)
    monkeypatch.setattr(screen_share, "_cursor_position", lambda: next(positions, (140, 10)))
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: True,
    )

    assert screen_share.execute_start_share(
        "Alt+S",
        300,
        250,
        movement_warning=lambda: warnings.append(True),
    ) is True
    assert warnings == [True]


def test_execute_start_share_without_target_sends_hotkey_only(monkeypatch):
    sent_hotkeys: list[str] = []
    clicks: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        screen_share,
        "send_hotkey",
        lambda hotkey: sent_hotkeys.append(hotkey) or True,
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share.execute_start_share("Alt+S") is True
    assert sent_hotkeys == ["Alt+S"]
    assert clicks == []


def test_execute_start_share_rejects_missing_hotkey_before_click(monkeypatch):
    clicks: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share.execute_start_share("", 300, 250) is False
    assert clicks == []


def test_linux_start_share_focuses_zoom_and_restores_previous_window(monkeypatch):
    session = object()
    events: list[str] = []
    monkeypatch.setattr(screen_share.sys, "platform", "linux")
    monkeypatch.setattr(
        screen_share,
        "_acquire_linux_zoom_focus",
        lambda: events.append("focus") or session,
    )
    monkeypatch.setattr(screen_share, "zoom_has_focus", lambda current: current is session)
    monkeypatch.setattr(
        screen_share,
        "send_hotkey",
        lambda _hotkey: events.append("hotkey") or True,
    )
    monkeypatch.setattr(
        screen_share,
        "restore_previous_focus",
        lambda current: events.append("restore") if current is session else None,
    )

    assert screen_share.execute_start_share("Alt+S") is True
    assert events == ["focus", "hotkey", "restore"]


def test_linux_stop_share_fails_closed_when_zoom_cannot_be_focused(monkeypatch):
    hotkeys: list[str] = []
    monkeypatch.setattr(screen_share.sys, "platform", "linux")
    monkeypatch.setattr(screen_share, "_acquire_linux_zoom_focus", lambda: None)
    monkeypatch.setattr(
        screen_share,
        "send_hotkey",
        lambda hotkey: hotkeys.append(hotkey) or True,
    )

    assert screen_share.execute_stop_share("Alt+S") is False
    assert hotkeys == []


def test_linux_start_share_restores_focus_when_zoom_loses_focus(monkeypatch):
    session = object()
    events: list[str] = []
    monkeypatch.setattr(screen_share.sys, "platform", "linux")
    monkeypatch.setattr(screen_share, "_acquire_linux_zoom_focus", lambda: session)
    monkeypatch.setattr(screen_share, "zoom_has_focus", lambda _current: False)
    monkeypatch.setattr(
        screen_share,
        "send_hotkey",
        lambda _hotkey: events.append("hotkey") or True,
    )
    monkeypatch.setattr(
        screen_share,
        "restore_previous_focus",
        lambda _current: events.append("restore"),
    )

    assert screen_share.execute_start_share("Alt+S") is False
    assert events == ["restore"]


def test_linux_click_sequence_uses_one_atomic_xdotool_repeat(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(screen_share.shutil, "which", lambda _name: "/usr/bin/xdotool")
    monkeypatch.setattr(screen_share.os, "environ", {"XDG_SESSION_TYPE": "x11"})
    monkeypatch.setattr(
        screen_share,
        "_xdotool_position",
        lambda _xdotool, _env: (40, 60),
    )

    def _run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(screen_share.subprocess, "run", _run)

    assert screen_share._clicks_linux(300, 250, count=2, interval_ms=120) is True
    assert commands == [
        ["/usr/bin/xdotool", "mousemove", "--sync", "300", "250"],
        [
            "/usr/bin/xdotool",
            "click",
            "--clearmodifiers",
            "--repeat",
            "2",
            "--delay",
            "120",
            "1",
        ],
        ["/usr/bin/xdotool", "mousemove", "--sync", "40", "60"],
    ]


def test_macos_click_sequence_moves_once_and_preserves_click_state(monkeypatch):
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)

    assert screen_share._run_macos_click_sequence(
        count=2,
        interval_ms=120,
        target=(300.0, 250.0),
        saved_pos=(40.0, 60.0),
        post_move=lambda point: events.append(("move", point)) or True,
        post_down=lambda point, state: events.append(("down", point, state)) or True,
        post_up=lambda point, state: events.append(("up", point, state)) or True,
    ) is True
    assert events == [
        ("move", (300.0, 250.0)),
        ("down", (300.0, 250.0), 1),
        ("up", (300.0, 250.0), 1),
        ("down", (300.0, 250.0), 2),
        ("up", (300.0, 250.0), 2),
        ("move", (40.0, 60.0)),
    ]


def test_macos_pyobjc_backend_sets_quartz_click_state(monkeypatch):
    click_states: list[tuple[int, int]] = []

    def _create_mouse_event(_source, event_type, _point, _button):
        return {"type": event_type}

    def _set_integer_field(event, _field, value):
        click_states.append((event["type"], value))

    quartz = SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        CGEventCreate=lambda _source: object(),
        CGEventGetLocation=lambda _event: (40.0, 60.0),
        CGEventCreateMouseEvent=_create_mouse_event,
        CGEventSetIntegerValueField=_set_integer_field,
        CGEventPost=lambda _tap, _event: None,
        kCGMouseButtonLeft=0,
        kCGHIDEventTap=0,
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGEventMouseMoved=5,
        kCGMouseEventClickState=1,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)

    assert screen_share._clicks_macos_pyobjc(300, 250, 2, 120) is True
    assert click_states == [(1, 1), (2, 1), (1, 2), (2, 2)]


def test_macos_ctypes_backend_sets_quartz_click_state(monkeypatch):
    click_states: list[tuple[int, int]] = []

    def _create_mouse_event(_source, event_type, _point, _button):
        return {"type": event_type}

    def _set_integer_field(event, _field, value):
        click_states.append((event["type"], value))

    app_services = SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        CGEventCreate=lambda _source: object(),
        CGEventGetLocation=lambda _event: screen_share._CGPOINT(40.0, 60.0),
        CGEventCreateMouseEvent=_create_mouse_event,
        CGEventSetIntegerValueField=_set_integer_field,
        CGEventPost=lambda _tap, _event: None,
        CFRelease=lambda _event: None,
    )
    monkeypatch.setattr(screen_share, "_load_application_services", lambda: app_services)
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)

    assert screen_share._clicks_macos_ctypes(300, 250, 2, 120) is True
    assert click_states == [(1, 1), (2, 1), (1, 2), (2, 2)]
