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
from solin.widgets.remote_control_setup_dialog import (
    RemoteControlSetupDialog,
    RemoteControlSetupPresentation,
    _fingerprint_display_text,
)
from solin.widgets.remote_sessions_popup import RemoteSessionsPopup, _RemoteSessionRow
from solin.widgets.settings.remote_control_section import RemoteControlSectionMixin


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_APP = QApplication.instance() or QApplication([])


class _CallbackSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _FakeQrSession:
    def __init__(self) -> None:
        self.ready = _CallbackSignal()
        self.failed = _CallbackSignal()
        self.started_with = ""
        self.close_calls = 0

    def start(self, value: str) -> bool:
        self.started_with = value
        return True

    def close(self) -> None:
        self.close_calls += 1


class _FakeQrFactory:
    def __init__(self) -> None:
        self.session = _FakeQrSession()
        self.parent = None

    def create(self, *, parent=None) -> _FakeQrSession:
        self.parent = parent
        return self.session


class _ValueSink:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def setText(self, value: str) -> None:
        self.value = value

    def setEnabled(self, value: bool) -> None:
        self.value = str(value)


class _RemoteStatusHarness(RemoteControlSectionMixin):
    def tr(self, source: str) -> str:
        return source


def _setup_presentation() -> RemoteControlSetupPresentation:
    return RemoteControlSetupPresentation(
        access_url="https://192.168.1.45:8765/remote/",
        setup_url="https://192.168.1.45:8765/remote/?setup=1",
        verification_code="ABCD-EFGH",
        fingerprint_sha256="AA:BB:CC:DD",
        certificate_der=b"certificate",
    )


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


def test_remote_toolbar_control_keeps_the_maximum_pill_within_its_fixed_surface() -> None:
    bridge = SimpleNamespace(
        _background_song_visible=True,
        _separator_visible=True,
        _obs_visible=True,
        _camera_visible=True,
        _zoom_visible=True,
        _remote_control_visible=True,
    )
    toolbar = SimpleNamespace(_bridge=bridge)
    source = Path("src/solin/qml/QuickAccessToolbar.qml").read_text(encoding="utf-8")

    assert QuickAccessToolbar._calc_pill_width(toolbar) == 240
    assert source.index("id: zoomMA") < source.index("id: remoteControlButton")
    assert source.index("id: remoteControlButton") < source.index("id: minMA")
    assert "Accessible.role: Accessible.Button" in source
    assert "bridge.remoteControlBadge" in source
    assert "bridge.remoteControlWarning" in source


def test_remote_setup_dialog_scans_the_dedicated_setup_route_and_closes_qr_work() -> None:
    factory = _FakeQrFactory()
    dialog = RemoteControlSetupDialog(_setup_presentation(), factory)
    accepted: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))

    assert factory.parent is dialog
    assert factory.session.started_with.endswith("/remote/?setup=1")

    dialog.accept()

    assert accepted == [True]
    assert factory.session.close_calls == 1


def test_remote_setup_copy_address_has_transient_confirmation_feedback() -> None:
    factory = _FakeQrFactory()
    dialog = RemoteControlSetupDialog(_setup_presentation(), factory)
    copy_button = dialog.findChild(QPushButton, "SetupCopyAddress")

    assert copy_button is not None
    assert copy_button.property("confirmed") is False

    copy_button.click()

    assert QApplication.clipboard().text() == _setup_presentation().setup_url
    assert copy_button.property("confirmed") is True


def test_remote_setup_dialog_dismissal_is_not_reported_as_completed() -> None:
    factory = _FakeQrFactory()
    dialog = RemoteControlSetupDialog(_setup_presentation(), factory)
    accepted: list[bool] = []
    rejected: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.rejected.connect(lambda: rejected.append(True))

    dialog.reject()

    assert accepted == []
    assert rejected == [True]
    assert factory.session.close_calls == 1


def test_settings_refresh_preserves_the_confirmed_remote_runtime_status() -> None:
    settings = _RemoteStatusHarness()
    settings._remote_interface_combo = SimpleNamespace(currentData=lambda: "ethernet")
    settings._remote_interfaces = {"ethernet": SimpleNamespace(ipv4_address="192.168.0.224")}
    settings._remote_control_settings = SimpleNamespace(enabled=lambda: True)
    settings._remote_control_credentials = SimpleNamespace(has_credentials=lambda: True)
    settings._remote_endpoint_label = _ValueSink()
    settings._remote_copy_url_btn = _ValueSink()
    settings._remote_runtime_status_known = True
    settings._remote_runtime_message = "Secure remote control is running."
    settings._remote_runtime_status_kind = "running"
    displayed: list[tuple[str, str]] = []
    settings._set_remote_status = lambda message, status: displayed.append((message, status))

    settings._refresh_remote_configuration_status()

    assert displayed == [("Secure remote control is running.", "running")]
    assert settings._remote_endpoint_label.value == "https://192.168.0.224:8765/remote/"


def test_remote_setup_fingerprint_adds_safe_wrap_points_without_changing_octets() -> None:
    fingerprint = ":".join(f"{value:02X}" for value in range(32))

    displayed = _fingerprint_display_text(fingerprint)

    assert "  " in displayed
    assert displayed.replace("  ", ":") == fingerprint
    assert max(len(group) for group in displayed.split("  ")) == 11


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
