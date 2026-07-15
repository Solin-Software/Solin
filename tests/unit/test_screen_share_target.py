from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from solin.core.integrations.automation import screen_share


class _NativeCall:
    def __init__(self, implementation):
        self._implementation = implementation

    def __call__(self, *args):
        return self._implementation(*args)


def _window(
    window_id: str,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    pid: int = 0,
) -> screen_share._ZoomWindowInfo:
    return screen_share._ZoomWindowInfo(
        window_id=window_id,
        owner="zoom.exe",
        x=x,
        y=y,
        width=width,
        height=height,
        pid=pid,
    )


def _bypass_linux_zoom_focus(monkeypatch) -> None:
    """Isolate share sequencing tests from the Linux window-manager boundary."""
    monkeypatch.setattr(screen_share, "_acquire_linux_zoom_focus", lambda: None)
    monkeypatch.setattr(
        screen_share,
        "_linux_zoom_focus_is_required",
        lambda: False,
    )


def _configure_detected_share_dialog(monkeypatch, dialog, clicks) -> None:
    _bypass_linux_zoom_focus(monkeypatch)
    window_calls = 0

    def _list_windows():
        nonlocal window_calls
        window_calls += 1
        return {} if window_calls == 1 else {dialog.window_id: dialog}

    monkeypatch.setattr(screen_share, "_list_zoom_windows", _list_windows)
    monkeypatch.setattr(
        screen_share,
        "_wait_for_new_zoom_window",
        lambda _initial, _monitor=None: dialog,
    )
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.ZOOM,
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: (
            screen_share._ShareDialogPresence.ABSENT
        ),
    )


def test_execute_start_share_clicks_absolute_configured_position(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    _configure_detected_share_dialog(monkeypatch, dialog, clicks)

    assert screen_share.execute_start_share("Alt+S", 300, 250) is True
    assert clicks == [(300, 250, 2, 120)]


def test_auto_share_uses_conservative_interaction_delays() -> None:
    assert screen_share.SHARE_DIALOG_INTERACTION_DELAY_MS == 550
    assert screen_share.SHARE_DIALOG_FIRST_CLICK_CONFIRMATION_MS == 450
    assert screen_share.SHARE_DIALOG_RETRY_CONFIRMATION_MS == 1100
    assert screen_share.SHARE_DIALOG_TARGET_READY_STABLE_SAMPLES == 3


def test_execute_start_share_rejects_target_outside_detected_dialog(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    _configure_detected_share_dialog(monkeypatch, dialog, clicks)

    assert screen_share.execute_start_share("Alt+S", 1200, 250) is False
    assert clicks == []


def test_execute_start_share_rechecks_dialog_after_safety_delay(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    _bypass_linux_zoom_focus(monkeypatch)
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: {})
    monkeypatch.setattr(
        screen_share,
        "_wait_for_new_zoom_window",
        lambda _initial, _monitor=None: dialog,
    )
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.ZOOM,
    )
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share.execute_start_share("Alt+S", 300, 250) is False
    assert clicks == []


def test_execute_start_share_warns_once_when_mouse_moves_during_wait(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    calls = {"windows": 0}
    positions = iter([(10, 10), (10, 10), (140, 10), (140, 10), (140, 10)])
    warnings: list[bool] = []

    def _list_windows():
        calls["windows"] += 1
        return {} if calls["windows"] == 1 else {"dialog": dialog}

    _bypass_linux_zoom_focus(monkeypatch)
    monkeypatch.setattr(screen_share, "_list_zoom_windows", _list_windows)
    monkeypatch.setattr(screen_share, "_cursor_position", lambda: next(positions, (140, 10)))
    monkeypatch.setattr(
        screen_share,
        "_sleep_with_mouse_monitoring",
        lambda _milliseconds, monitor=None: monitor.check() if monitor else None,
    )
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.ZOOM,
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: True,
    )
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: (
            screen_share._ShareDialogPresence.ABSENT
        ),
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
    _bypass_linux_zoom_focus(monkeypatch)
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


def test_execute_start_share_retries_once_when_same_dialog_remains(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    presences = iter(
        [
            screen_share._ShareDialogPresence.PRESENT,
            screen_share._ShareDialogPresence.ABSENT,
        ]
    )
    confirmation_timeouts: list[int] = []
    clicks: list[tuple[int, int, int, int]] = []
    _configure_detected_share_dialog(monkeypatch, dialog, clicks)

    def _wait_for_exit(_dialog, timeout_ms, _monitor=None):
        confirmation_timeouts.append(timeout_ms)
        return next(presences)

    monkeypatch.setattr(screen_share, "_wait_for_share_dialog_exit", _wait_for_exit)

    assert screen_share.execute_start_share("Alt+S", 300, 250) is True
    assert clicks == [(300, 250, 2, 120), (300, 250, 2, 120)]
    assert confirmation_timeouts == [
        screen_share.SHARE_DIALOG_FIRST_CLICK_CONFIRMATION_MS,
        screen_share.SHARE_DIALOG_RETRY_CONFIRMATION_MS,
    ]


def test_execute_start_share_fails_without_retry_when_dialog_state_is_unknown(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    _configure_detected_share_dialog(monkeypatch, dialog, clicks)
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: screen_share._ShareDialogPresence.UNKNOWN,
    )

    assert screen_share.execute_start_share("Alt+S", 300, 250) is False
    assert clicks == [(300, 250, 2, 120)]


def test_execute_start_share_fails_after_one_unsuccessful_retry(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    _configure_detected_share_dialog(monkeypatch, dialog, clicks)
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: screen_share._ShareDialogPresence.PRESENT,
    )

    assert screen_share.execute_start_share("Alt+S", 300, 250) is False
    assert clicks == [(300, 250, 2, 120), (300, 250, 2, 120)]


def test_retry_revalidates_dialog_before_second_click(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    validations = iter([dialog, None])
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda _dialog, _x, _y: next(validations),
    )
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: (
            screen_share._ShareDialogPresence.PRESENT
        ),
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.ZOOM,
    )

    assert screen_share._click_share_target_with_retry(300, 250, dialog) is False
    assert clicks == [(300, 250, 2, 120)]


def test_target_readiness_requires_three_consecutive_zoom_samples(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    ownership = iter(
        [
            screen_share._ClickTargetOwnership.OTHER,
            screen_share._ClickTargetOwnership.ZOOM,
            screen_share._ClickTargetOwnership.ZOOM,
            screen_share._ClickTargetOwnership.ZOOM,
        ]
    )
    times = iter([0.0, 0.01, 0.02, 0.03])
    sleeps: list[int] = []
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda current, _x, _y: current,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: next(ownership),
    )
    monkeypatch.setattr(screen_share.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        screen_share,
        "_sleep_with_mouse_monitoring",
        lambda duration_ms, _monitor=None: sleeps.append(duration_ms),
    )

    assert screen_share._wait_for_zoom_click_target(dialog, 300, 250) is dialog
    assert sleeps == [screen_share.SHARE_DIALOG_TARGET_READY_POLL_INTERVAL_MS] * 3


def test_target_readiness_times_out_while_another_window_owns_the_point(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda current, _x, _y: current,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.OTHER,
    )
    monkeypatch.setattr(screen_share.time, "monotonic", lambda: next(times))

    assert screen_share._wait_for_zoom_click_target(dialog, 300, 250) is None


def test_target_readiness_fails_closed_when_native_probe_is_unavailable(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    sleeps: list[int] = []
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda current, _x, _y: current,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.UNKNOWN,
    )
    monkeypatch.setattr(
        screen_share,
        "_sleep_with_mouse_monitoring",
        lambda duration_ms, _monitor=None: sleeps.append(duration_ms),
    )

    assert screen_share._wait_for_zoom_click_target(dialog, 300, 250) is None
    assert sleeps == []


def test_first_click_is_suppressed_if_zoom_loses_target_ownership(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda current, _x, _y: current,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: screen_share._ClickTargetOwnership.OTHER,
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share._click_share_target_with_retry(300, 250, dialog) is False
    assert clicks == []


def test_retry_is_suppressed_if_zoom_loses_target_ownership(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    ownership = iter(
        [
            screen_share._ClickTargetOwnership.ZOOM,
            screen_share._ClickTargetOwnership.OTHER,
        ]
    )
    monkeypatch.setattr(
        screen_share,
        "_current_safe_share_dialog",
        lambda current, _x, _y: current,
    )
    monkeypatch.setattr(
        screen_share,
        "_click_target_ownership",
        lambda _dialog, _x, _y: next(ownership),
    )
    monkeypatch.setattr(
        screen_share,
        "_wait_for_share_dialog_exit",
        lambda _dialog, _timeout_ms, _monitor=None: (
            screen_share._ShareDialogPresence.PRESENT
        ),
    )
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share._click_share_target_with_retry(300, 250, dialog) is False
    assert clicks == [(300, 250, 2, 120)]


def test_win32_target_probe_requires_the_exact_detected_root_window(monkeypatch):
    roots = {222: 111, 333: 999}
    recipient = {"value": 222}
    user32 = SimpleNamespace(
        WindowFromPoint=_NativeCall(lambda _point: recipient["value"]),
        GetAncestor=_NativeCall(lambda hwnd, _flag: roots[hwnd]),
    )
    monkeypatch.setattr(
        screen_share.ctypes,
        "WinDLL",
        lambda _name, use_last_error=True: user32,
    )
    dialog = _window("111", x=100, y=100, width=801, height=601)

    assert screen_share._click_target_ownership_win32(dialog, 300, 250) is (
        screen_share._ClickTargetOwnership.ZOOM
    )

    recipient["value"] = 333
    assert screen_share._click_target_ownership_win32(dialog, 300, 250) is (
        screen_share._ClickTargetOwnership.OTHER
    )


def test_macos_target_probe_uses_accessibility_hit_test_pid(monkeypatch):
    dialog = _window(
        "111",
        x=100,
        y=100,
        width=801,
        height=601,
        pid=42,
    )
    recipient = {"pid": 42}
    monkeypatch.setattr(
        screen_share,
        "_macos_accessibility_pid_at_point",
        lambda _x, _y: recipient["pid"],
    )

    assert screen_share._click_target_ownership_macos(dialog, 300, 250) is (
        screen_share._ClickTargetOwnership.ZOOM
    )

    recipient["pid"] = 99
    assert screen_share._click_target_ownership_macos(dialog, 300, 250) is (
        screen_share._ClickTargetOwnership.OTHER
    )


def test_macos_accessibility_probe_returns_topmost_element_pid(monkeypatch):
    releases: list[int] = []

    def _copy_element(_system, _x, _y, element_pointer):
        element_pointer._obj.value = 200
        return 0

    def _get_pid(_element, pid_pointer):
        pid_pointer._obj.value = 42
        return 0

    app_services = SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        AXUIElementCreateSystemWide=lambda: 100,
        AXUIElementCopyElementAtPosition=_copy_element,
        AXUIElementGetPid=_get_pid,
        CFRelease=releases.append,
    )
    monkeypatch.setattr(
        screen_share,
        "_load_application_services",
        lambda: app_services,
    )

    assert screen_share._macos_accessibility_pid_at_point(300, 250) == 42
    assert releases == [200, 100]


def test_linux_target_probe_dispatches_to_x11_window_verifier(monkeypatch):
    dialog = _window("111", x=100, y=100, width=801, height=601)
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(screen_share.sys, "platform", "linux")
    monkeypatch.setattr(
        screen_share,
        "point_is_owned_by_window",
        lambda window_id, x, y: calls.append((window_id, x, y)) or True,
    )

    assert screen_share._click_target_ownership(dialog, 300, 250) is (
        screen_share._ClickTargetOwnership.ZOOM
    )
    assert calls == [("111", 300, 250)]


def test_share_dialog_exit_probe_tracks_only_detected_window(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    other = _window("other", x=100, y=100, width=801, height=601)
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: {"other": other})

    assert screen_share._wait_for_share_dialog_exit(dialog, 250) is (
        screen_share._ShareDialogPresence.ABSENT
    )


def test_share_dialog_exit_probe_polls_until_success_before_deadline(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    window_snapshots = iter(
        [
            {"dialog": dialog},
            {"dialog": dialog},
            {},
        ]
    )
    times = iter([0.0, 0.01, 0.06])
    sleeps: list[int] = []
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: next(window_snapshots))
    monkeypatch.setattr(screen_share.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        screen_share,
        "_sleep_with_mouse_monitoring",
        lambda duration_ms, _monitor=None: sleeps.append(duration_ms),
    )

    assert screen_share._wait_for_share_dialog_exit(dialog, 2000) is (
        screen_share._ShareDialogPresence.ABSENT
    )
    assert sleeps == [screen_share.SHARE_DIALOG_POLL_INTERVAL_MS] * 2


def test_share_dialog_exit_probe_reports_same_window_after_timeout(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    times = iter([0.0, 0.3])
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: {"dialog": dialog})
    monkeypatch.setattr(screen_share.time, "monotonic", lambda: next(times))

    assert screen_share._wait_for_share_dialog_exit(dialog, 250) is (
        screen_share._ShareDialogPresence.PRESENT
    )


def test_share_dialog_exit_probe_fails_safe_when_enumeration_is_unavailable(monkeypatch):
    dialog = _window("dialog", x=100, y=100, width=801, height=601)
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: None)

    assert screen_share._wait_for_share_dialog_exit(dialog, 250) is (
        screen_share._ShareDialogPresence.UNKNOWN
    )


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
