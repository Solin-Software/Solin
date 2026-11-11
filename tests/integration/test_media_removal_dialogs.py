import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from solin.core.foundation import constants
from solin.ui.dialogs.media_removal import (
    ContainerRemovalChoice,
    confirm_additional_media_removal,
    confirm_container_removal,
)


@pytest.mark.parametrize("reply, expected", [
    (QMessageBox.StandardButton.Yes, ContainerRemovalChoice.KEEP_MEDIA),
    (QMessageBox.StandardButton.No, ContainerRemovalChoice.CANCEL),
])
def test_disabled_flag_keeps_existing_confirmation_contract(monkeypatch, reply, expected):
    calls = []
    monkeypatch.setattr(constants, "CONFIRM_CONTAINER_MEDIA_DELETION", False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: calls.append(args) or reply)
    assert confirm_container_removal(None, "Scope", subsection=True) is expected
    assert calls[0][3] == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No


def test_disabled_flag_names_the_requested_container(monkeypatch):
    calls = []
    monkeypatch.setattr(constants, "CONFIRM_CONTAINER_MEDIA_DELETION", False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: calls.append(args))
    for subsection in (False, True):
        confirm_container_removal(None, "Scope", subsection=subsection)

    assert "subsection" in calls[1][2].lower()
    assert "section" in calls[0][2].lower()
    assert calls[0][2] != calls[1][2]


@pytest.mark.parametrize("role, expected", [
    (QMessageBox.ButtonRole.DestructiveRole, ContainerRemovalChoice.DELETE_MEDIA),
    (QMessageBox.ButtonRole.ActionRole, ContainerRemovalChoice.KEEP_MEDIA),
    (QMessageBox.ButtonRole.RejectRole, ContainerRemovalChoice.CANCEL),
])
def test_enabled_flag_offers_distinct_choices_and_defaults_to_cancel(monkeypatch, role, expected):
    monkeypatch.setattr(constants, "CONFIRM_CONTAINER_MEDIA_DELETION", True)
    seen = []

    def choose():
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QMessageBox):
            return
        cancel = dialog.button(QMessageBox.StandardButton.Cancel)
        seen.append((dialog.defaultButton() is cancel, dialog.escapeButton() is cancel))
        next(button for button in dialog.buttons() if dialog.buttonRole(button) == role).click()

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(choose)
    timer.start()
    try:
        assert confirm_container_removal(None, "Scope", subsection=False) is expected
    finally:
        timer.stop()
    assert seen == [(True, True)]


def test_additional_media_confirmation_has_safe_default_and_scope_count(monkeypatch):
    calls = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args: calls.append(args) or QMessageBox.StandardButton.Cancel,
    )
    assert not confirm_additional_media_removal(None, "Scope", 7)
    assert "Scope" in calls[0][2] and "7" in calls[0][2]
    assert calls[0][-1] == QMessageBox.StandardButton.Cancel


def test_additional_media_confirmation_preserves_placeholder_text_in_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args: calls.append(args) or QMessageBox.StandardButton.Cancel,
    )
    assert not confirm_additional_media_removal(None, "Video {count}", 3)
    assert '"Video {count}"' in calls[0][2]
    assert "Media items: 3" in calls[0][2]
