from __future__ import annotations

from solin.core.integrations.automation import screen_share


def _window(
    window_id: str,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    is_share_dialog: bool = False,
) -> screen_share._ZoomWindowInfo:
    return screen_share._ZoomWindowInfo(
        window_id=window_id,
        owner="zoom.exe",
        x=x,
        y=y,
        width=width,
        height=height,
        is_share_dialog=is_share_dialog,
    )


def test_find_zoom_share_dialog_at_point_uses_most_specific_window(monkeypatch):
    meeting = _window("meeting", x=0, y=0, width=1400, height=900)
    dialog = _window("dialog", x=200, y=100, width=800, height=600)
    monkeypatch.setattr(
        screen_share,
        "_list_zoom_windows",
        lambda: {meeting.window_id: meeting, dialog.window_id: dialog},
    )

    assert screen_share.find_zoom_share_dialog_bounds_at_point(500, 400) == (
        200,
        100,
        800,
        600,
    )


def test_find_zoom_share_dialog_at_point_rejects_non_zoom_area(monkeypatch):
    dialog = _window("dialog", x=-1200, y=100, width=800, height=600)
    monkeypatch.setattr(
        screen_share,
        "_list_zoom_windows",
        lambda: {dialog.window_id: dialog},
    )

    assert screen_share.find_zoom_share_dialog_bounds_at_point(100, 100) is None


def test_find_zoom_share_dialog_can_require_identified_dialog(monkeypatch):
    meeting = _window("meeting", x=0, y=0, width=1400, height=900)
    dialog = _window(
        "dialog",
        x=200,
        y=100,
        width=800,
        height=600,
        is_share_dialog=True,
    )
    monkeypatch.setattr(
        screen_share,
        "_list_zoom_windows",
        lambda: {meeting.window_id: meeting, dialog.window_id: dialog},
    )

    assert screen_share.find_zoom_share_dialog_bounds_at_point(
        500,
        400,
        require_identified_dialog=True,
    ) == (200, 100, 800, 600)


def test_execute_start_share_resolves_target_against_detected_dialog(monkeypatch):
    dialog = _window("dialog", x=-1200, y=100, width=801, height=601)
    clicks: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(screen_share, "_list_zoom_windows", lambda: {})
    monkeypatch.setattr(
        screen_share,
        "_wait_for_new_zoom_window",
        lambda _initial: dialog,
    )
    monkeypatch.setattr(screen_share, "send_hotkey", lambda _hotkey: True)
    monkeypatch.setattr(screen_share.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        screen_share,
        "send_virtual_clicks",
        lambda x, y, count, interval_ms: clicks.append((x, y, count, interval_ms))
        or True,
    )

    assert screen_share.execute_start_share("Alt+S", 0.25, 0.75) is True
    assert clicks == [(-1000, 550, 2, 120)]


def test_execute_start_share_rejects_missing_target_before_hotkey(monkeypatch):
    sent_hotkeys: list[str] = []
    monkeypatch.setattr(
        screen_share,
        "send_hotkey",
        lambda hotkey: sent_hotkeys.append(hotkey) or True,
    )

    assert screen_share.execute_start_share("Alt+S", -1.0, -1.0) is False
    assert sent_hotkeys == []
