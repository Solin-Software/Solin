"""Native popup lifecycle, including interrupted display and destruction."""
from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from solin.core.remote_control.security import RemoteSessionInfo
from solin.widgets.remote_sessions_popup import RemoteSessionsPopup
from tests._qt import wait_until


@pytest.fixture
def popup_host():
    anchor = QWidget()
    popup = None
    try:
        available = anchor.screen().availableGeometry()
        anchor.setGeometry(available.left() + 40, available.bottom() - 80, 240, 40)
        anchor.show()
        popup = RemoteSessionsPopup(anchor)
        popup.set_runtime_state(True, True, "running")
        popup.set_sessions((RemoteSessionInfo(
            management_id="one", principal="operator", browser="Chrome",
            platform="Android", client_mode="standalone", remote_address="192.168.1.45",
            created_at_utc=1_721_130_000.0, last_activity_at_utc=1_721_130_400.0,
            connected_socket_count=1,
        ),))
        yield anchor, popup
    finally:
        if popup is not None and shiboken6.isValid(popup):
            popup.close()
        if shiboken6.isValid(anchor):
            anchor.close()
            anchor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_popup_hide_stops_an_inflight_fade(popup_host):
    anchor, popup = popup_host
    popup.show_above(anchor)
    QApplication.processEvents()
    assert popup._fade.state() == QAbstractAnimation.State.Running

    popup.hide()

    assert popup._fade.state() == QAbstractAnimation.State.Stopped
    assert not popup._activity_timer.isActive()
    assert popup._opacity.opacity() == 1.0


def test_popup_dismissal_before_display_completion_cancels_the_open(popup_host):
    anchor, popup = popup_host
    popup.show_above(anchor)
    popup.hide()
    QApplication.processEvents()

    assert not popup.isVisible()
    assert popup._fade.state() == QAbstractAnimation.State.Stopped
    assert popup._opacity.opacity() == 1.0


def test_popup_reopens_after_an_interrupted_fade(popup_host):
    anchor, popup = popup_host
    for _ in range(3):
        popup.show_above(anchor)
        QApplication.processEvents()
        popup.hide()

    popup.show_above(anchor)
    wait_until(
        lambda: not popup._show_timer.isActive()
        and popup._fade.state() == QAbstractAnimation.State.Stopped,
        description="popup display and fade completion",
    )

    assert popup.isVisible()
    assert popup._fade.state() == QAbstractAnimation.State.Stopped
    assert popup._opacity.opacity() == 1.0


def test_popup_parent_destruction_cancels_pending_display_completion(popup_host):
    anchor, popup = popup_host
    popup.show_above(anchor)
    anchor.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()

    assert not shiboken6.isValid(popup)
