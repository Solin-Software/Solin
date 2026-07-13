from __future__ import annotations

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
