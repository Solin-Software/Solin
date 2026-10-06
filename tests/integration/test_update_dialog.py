from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication
from solin.core.releases.manifest import ReleaseAsset
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import ReleaseNote, UpdateAction, UpdateInfo
from solin.ui.dialogs.update import UpdateDialog

_APP = QApplication.instance() or QApplication([])


def dialog(action=UpdateAction.INSTALL, launch=lambda path: None):
    version = ReleaseVersion.parse("26.32.0")
    info = UpdateInfo(
        action,
        version,
        ReleaseAsset("windows", "x86_64", "installer", "Solin.exe", 3, "a" * 64, "10"),
        "https://github.com/file",
        (ReleaseNote(version, "- Changes", "en"),),
    )
    return UpdateDialog(info, downloader_factory=lambda *args: None, launch_installer=launch)


def test_download_completion_requires_explicit_installation():
    launches = []
    instance = dialog(launch=launches.append)
    instance._on_download_done("Solin.exe")
    assert launches == []
    assert instance._btn_action.text() == "Install and restart"
    assert instance._btn_cancel.isEnabled()
    instance._on_action()
    assert launches == ["Solin.exe"]


def test_dialog_renders_notes_but_blocks_images():
    instance = dialog()
    browser = instance._changelog_browser
    assert "Changes" in browser.toPlainText()
    assert browser.loadResource(QTextDocument.ResourceType.ImageResource, browser.source()) is None


def test_installer_failure_allows_retry():
    def fail(path):
        raise RuntimeError("scope unavailable")

    instance = dialog(launch=fail)
    instance._on_download_done("Solin.exe")
    instance._on_action()
    assert instance._btn_action.isEnabled()
    assert instance._status_label.text() == "Failed to launch installer."


def test_failed_download_allows_retry():
    instance = dialog()
    instance._on_download_failed("disk full")
    assert instance._btn_action.isEnabled()
    assert instance._btn_action.text() == "Try again"
