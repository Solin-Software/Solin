from __future__ import annotations

import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QEvent, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from solin.bootstrap.application_window import ApplicationWindow
from solin.core.releases.manifest import ReleaseAsset
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.notification_policy import Notification
from solin.core.remote.update_policy import UpdateAction, UpdateInfo
from solin.ui.dialogs.notifications import NotificationDialog
from solin.ui.dialogs.positioning import center_dialog_on_parent
from solin.ui.dialogs.update import UpdateDialog
from solin.ui.qml.media_destination import MediaDestinationBridge, MediaDestinationDialog


@pytest.fixture
def owner_window():
    window = ApplicationWindow(
        width=640,
        height=600,
        geometry=QByteArray(),
        save_geometry=lambda _geometry: None,
        pending_files=[],
    )
    try:
        host = window.content_parent()
        runtime = QWidget(host)
        host.layout().addWidget(runtime)
        host.layout().setCurrentWidget(runtime)
        window.show()
        QApplication.processEvents()
        yield window, runtime
    finally:
        window.close()
        window.deleteLater()


def _dialog(kind, parent):
    if kind == "notification":
        return NotificationDialog(Notification("test", "info", "Notice", "Details"), None, parent)
    if kind == "destination":
        bridge = MediaDestinationBridge(
            media_title="Sample", item_count=1, can_play=False, playlists=()
        )
        return MediaDestinationDialog(bridge, parent)
    info = UpdateInfo(
        UpdateAction.INSTALL,
        ReleaseVersion.parse("26.32.0"),
        ReleaseAsset("windows", "x86_64", "installer", "Solin.exe", 3, "a" * 64, "10"),
        "https://example.test/update",
    )
    return UpdateDialog(
        info,
        parent,
        downloader_factory=lambda *args: None,
        launch_installer=lambda _path: None,
    )


@pytest.mark.parametrize("kind", ["notification", "update"])
@pytest.mark.parametrize("embedded", [False, True], ids=["window-parent", "embedded-parent"])
@pytest.mark.parametrize("position", [(40, 30), (110, 90)])
def test_dialog_centers_its_frame_on_the_visible_window(owner_window, kind, embedded, position):
    window, runtime = owner_window
    window.move(*position)
    QApplication.processEvents()
    instance = _dialog(kind, runtime if embedded else window)
    try:
        if kind == "notification":
            instance.show_centered_on_parent()
        else:
            instance.show()
        QApplication.processEvents()
        offset = instance.frameGeometry().center() - window.frameGeometry().center()
        assert abs(offset.x()) <= 1
        assert abs(offset.y()) <= 1
        assert instance.parentWidget() is (runtime if embedded else window)
    finally:
        instance.close()
        if kind != "notification":
            instance.deleteLater()


@pytest.mark.parametrize("kind", ["notification", "update"])
@pytest.mark.parametrize("has_parent", [True, False], ids=["hidden-parent", "no-parent"])
def test_dialog_without_a_visible_owner_centers_on_the_available_screen(kind, has_parent):
    parent = QWidget() if has_parent else None
    instance = _dialog(kind, parent)
    try:
        if kind == "notification":
            instance.show_centered_on_parent()
        else:
            instance.show()
        QApplication.processEvents()
        offset = (
            instance.frameGeometry().center()
            - QGuiApplication.primaryScreen().availableGeometry().center()
        )
        assert abs(offset.x()) <= 1
        assert abs(offset.y()) <= 1
    finally:
        instance.close()
        if kind != "notification":
            instance.deleteLater()
        if parent is not None:
            parent.deleteLater()


def test_positioning_uses_the_visible_owners_screen(owner_window, monkeypatch):
    window, runtime = owner_window
    available = QRect(-1920, -100, 1920, 1080)

    class Screen:
        def availableGeometry(self):
            return available

    monkeypatch.setattr(window, "screen", lambda: Screen())
    monkeypatch.setattr(QGuiApplication, "primaryScreen", lambda: None)
    instance = QDialog(runtime)
    try:
        instance.resize(200, 100)
        instance.show()
        center_dialog_on_parent(instance)
        assert available.contains(instance.frameGeometry())
        assert instance.frameGeometry().right() == available.right()
    finally:
        instance.close()
        instance.deleteLater()


def test_positioning_does_not_move_a_hidden_dialog(owner_window):
    window, runtime = owner_window
    instance = QDialog(runtime)
    try:
        instance.move(17, 23)
        original_position = instance.pos()
        center_dialog_on_parent(instance)
        assert instance.pos() == original_position
    finally:
        instance.deleteLater()


def test_positioning_without_a_parent_or_screen_keeps_the_current_position(monkeypatch):
    instance = QDialog()
    try:
        instance.show()
        instance.move(17, 23)
        original_position = instance.pos()
        monkeypatch.setattr(QGuiApplication, "primaryScreen", lambda: None)
        center_dialog_on_parent(instance)
        assert instance.pos() == original_position
    finally:
        instance.close()
        instance.deleteLater()


@pytest.mark.parametrize("kind", ["notification", "update"])
def test_pending_positioning_does_not_move_a_dialog_hidden_before_the_next_event(
    owner_window, kind
):
    _window, runtime = owner_window
    instance = _dialog(kind, runtime)
    try:
        if kind == "notification":
            instance.show_centered_on_parent()
        else:
            instance.show()
        instance.hide()
        original_position = instance.pos()
        QApplication.processEvents()
        assert instance.pos() == original_position
    finally:
        instance.close()
        if kind != "notification":
            instance.deleteLater()


@pytest.mark.parametrize("kind", ["update", "destination"])
def test_pending_positioning_is_cancelled_when_the_owner_is_destroyed(kind):
    owner = QWidget()
    destroyed = []
    owner.destroyed.connect(lambda: destroyed.append(True))
    try:
        owner.show()
        instance = _dialog(kind, owner)
        instance.show()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed
        QApplication.processEvents()
    finally:
        if not destroyed:
            owner.close()
            owner.deleteLater()
