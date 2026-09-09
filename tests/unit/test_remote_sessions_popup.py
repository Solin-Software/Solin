from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QCoreApplication, QRect
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from solin.core.remote_control.security import RemoteSessionInfo
from solin.styles.icons import ICON_REMOTE_CONTROL, make_icon
from solin.ui.controls import ButtonConfirmationFeedback
from solin.ui.qml.quick_toolbar import QuickToolbarBridge
from solin.widgets.quick_access_toolbar import QuickAccessToolbar
from solin.widgets.remote_sessions_popup import RemoteSessionsPopup, _RemoteSessionRow


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_APP = QApplication.instance() or QApplication([])


def _session(
    management_id: str,
    *,
    browser: str,
    platform: str,
    sockets: int,
) -> RemoteSessionInfo:
    return RemoteSessionInfo(
        management_id=management_id,
        principal="operator",
        browser=browser,
        platform=platform,
        client_mode="standalone" if sockets else "browser",
        remote_address="192.168.1.45",
        created_at_utc=1_721_130_000.0,
        last_activity_at_utc=1_721_130_400.0,
        connected_socket_count=sockets,
    )


def test_quick_toolbar_bridge_exposes_remote_state_without_secret_material() -> None:
    bridge = QuickToolbarBridge()
    clicks: list[bool] = []
    bridge.remoteControlClicked.connect(lambda: clicks.append(True))

    bridge.set_remote_control_visible(True)
    bridge.set_remote_control_icon_color("#388bfd")
    bridge.set_remote_control_badge("2")
    bridge.set_remote_control_warning(False)
    bridge.set_remote_control_tooltip("Remote control · 2 signed-in devices")
    bridge.onRemoteControlClicked()

    assert bridge.remoteControlVisible is True
    assert bridge.remoteControlIconColor == "388bfd"
    assert bridge.remoteControlBadge == "2"
    assert bridge.remoteControlWarning is False
    assert bridge.remoteControlTooltip == "Remote control · 2 signed-in devices"
    assert clicks == [True]


def test_quick_toolbar_bridge_exposes_solin_scenes_independently_from_obs() -> None:
    bridge = QuickToolbarBridge()
    clicks: list[bool] = []
    bridge.scenesClicked.connect(lambda: clicks.append(True))

    bridge.set_scenes_visible(True)
    bridge.set_scenes_icon_color("#388bfd")
    bridge.set_scenes_recording(True)
    bridge.set_scenes_tooltip("Solin scenes · engine ready")
    bridge.onScenesClicked()

    assert bridge.scenesVisible is True
    assert bridge.scenesIconColor == "388bfd"
    assert bridge.scenesRecording is True
    assert bridge.scenesTooltip == "Solin scenes · engine ready"
    assert clicks == [True]


def test_quick_toolbar_reflects_shared_program_recording_activity() -> None:
    bridge = QuickToolbarBridge()
    toolbar = SimpleNamespace(_bridge=bridge)

    QuickAccessToolbar._sync_program_recording_state(toolbar, True)
    assert bridge.scenesRecording is True

    QuickAccessToolbar._sync_program_recording_state(toolbar, False)
    assert bridge.scenesRecording is False


def test_remote_toolbar_control_keeps_the_maximum_pill_within_its_fixed_surface() -> None:
    bridge = SimpleNamespace(
        _background_song_visible=True,
        _separator_visible=True,
        _obs_visible=True,
        _scenes_visible=True,
        _camera_visible=False,
        _zoom_visible=True,
        _remote_control_visible=True,
    )
    toolbar = SimpleNamespace(_bridge=bridge)
    source = Path("src/solin/qml/QuickAccessToolbar.qml").read_text(encoding="utf-8")

    assert QuickAccessToolbar._calc_pill_width(toolbar) == 240
    assert source.index("id: obsMA") < source.index("id: scenesMA")
    assert source.index("id: scenesMA") < source.index("id: zoomMA")
    assert source.index("id: zoomMA") < source.index("id: remoteControlButton")
    assert source.index("id: remoteControlButton") < source.index("id: minMA")
    assert "Accessible.role: Accessible.Button" in source
    assert "bridge.remoteControlBadge" in source
    assert "bridge.remoteControlWarning" in source
    assert "bridge.scenesRecording" in source


def test_remote_sessions_popup_distinguishes_connected_and_signed_in_sessions() -> None:
    popup = RemoteSessionsPopup()
    sessions = (
        _session("connected", browser="Chrome", platform="Android", sockets=1),
        _session("signed-in", browser="Safari", platform="iPhone", sockets=0),
    )

    popup.set_runtime_state(True, True, "Secure remote control is running.")
    popup.set_sessions(sessions)

    rows = popup.findChildren(_RemoteSessionRow)
    assert {row.session.management_id for row in rows} == {"connected", "signed-in"}
    assert popup.findChild(QLabel, "RemoteSessionsCount").text() == "2"
    states = {
        label.objectName(): label.text()
        for label in popup.findChildren(QLabel)
        if label.objectName() in {"RemoteSessionConnected", "RemoteSessionSignedIn"}
    }
    assert states == {
        "RemoteSessionConnected": "Connected now",
        "RemoteSessionSignedIn": "Signed in",
    }


def test_remote_sessions_popup_uses_spacious_stable_geometry() -> None:
    popup = RemoteSessionsPopup()
    popup.set_runtime_state(True, True, "running")
    popup.set_sessions((_session("one", browser="Chrome", platform="Android", sockets=1),))

    assert popup.width() == 392
    assert popup.findChild(_RemoteSessionRow).minimumHeight() == 88
    assert popup.height() == popup.sizeHint().height()
    count = popup.findChild(QLabel, "RemoteSessionsCount")
    assert count.width() == count.height() == 24


def test_remote_sessions_popup_position_avoids_the_anchor_and_screen_edges() -> None:
    anchor = QRect(440, 700, 240, 40)
    available = QRect(0, 0, 1080, 760)

    x, y = RemoteSessionsPopup._popup_position(anchor, available, 392, 230)

    popup_rect = QRect(x, y, 392, 230)
    assert not popup_rect.intersects(anchor)
    assert available.adjusted(8, 8, -8, -8).contains(popup_rect)


def test_remote_sessions_popup_first_show_uses_its_final_height_above_anchor() -> None:
    anchor = QWidget()
    anchor.setGeometry(240, 560, 240, 40)
    anchor.show()
    popup = RemoteSessionsPopup(anchor)
    popup.set_runtime_state(True, True, "running")
    popup.set_sessions((_session("one", browser="Chrome", platform="Android", sockets=1),))

    popup.show_above(anchor)
    QApplication.processEvents()

    anchor_top = anchor.mapToGlobal(anchor.rect().topLeft()).y()
    assert popup.geometry().bottom() < anchor_top
    assert popup.height() == popup.sizeHint().height()

    popup.hide()
    anchor.close()


def test_remote_sessions_popup_confirms_granular_and_global_revocation() -> None:
    popup = RemoteSessionsPopup()
    session = _session("target", browser="Chrome", platform="Android", sockets=1)
    granular: list[str] = []
    global_requests: list[bool] = []
    popup.disconnect_requested.connect(granular.append)
    popup.disconnect_all_requested.connect(lambda: global_requests.append(True))
    popup.set_runtime_state(True, True, "running")
    popup.set_sessions((session,))

    row = popup.findChild(_RemoteSessionRow)
    assert row is not None
    row.findChild(QPushButton, "RemoteSessionDisconnect").click()
    confirm = next(
        button for button in row.findChildren(QPushButton) if button.property("destructive") is True
    )
    confirm.click()
    assert granular == ["target"]

    popup.set_revocation_result("target", False)
    error = row.findChild(QLabel, "RemoteSessionError")
    assert error.isVisibleTo(row)
    assert "Could not disconnect" in error.text()

    disconnect_all = popup.findChild(QPushButton, "RemoteSessionsDisconnectAll")
    disconnect_all.click()
    global_confirm = next(
        button
        for button in popup.findChildren(QPushButton)
        if button is not confirm
        and button.property("destructive") is True
        and button.text() == "Disconnect all"
    )
    global_confirm.click()
    assert global_requests == [True]


def test_remote_sessions_popup_palette_change_does_not_reapply_styles_recursively(
    monkeypatch,
) -> None:
    popup = RemoteSessionsPopup()
    calls: list[bool] = []
    monkeypatch.setattr(popup, "apply_theme", lambda: calls.append(True))

    QCoreApplication.sendEvent(popup, QEvent(QEvent.Type.PaletteChange))

    assert calls == []


def test_remote_control_icon_keeps_clear_pixels_at_the_right_viewbox_edge() -> None:
    image = make_icon(ICON_REMOTE_CONTROL, 14, "#ffffff").pixmap(14, 14).toImage()

    assert all(QColor.fromRgba(image.pixel(13, y)).alpha() == 0 for y in range(14))


def test_button_confirmation_feedback_preserves_text_and_resets_state() -> None:
    button = QPushButton("Copy address")
    feedback = ButtonConfirmationFeedback(
        button,
        idle_icon=QIcon,
        confirmed_icon=QIcon,
    )

    feedback.confirm()
    assert feedback.confirmed is True
    assert button.property("confirmed") is True
    assert button.text() == "Copy address"

    feedback.reset()
    assert feedback.confirmed is False
    assert button.property("confirmed") is False
    assert button.text() == "Copy address"
