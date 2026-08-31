from __future__ import annotations

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QLabel

from solin.core.remote.update_policy import (
    ReleaseNote,
    ReleaseVersion,
    UpdateInfo,
    UpdateKind,
)
from solin.ui.dialogs.update import UpdateDialog


_APP = QApplication.instance() or QApplication([])


def _version(value: str) -> ReleaseVersion:
    parsed = ReleaseVersion.parse(value)
    assert parsed is not None
    return parsed


def _dialog(info: UpdateInfo) -> UpdateDialog:
    return UpdateDialog(
        info,
        patch_downloader_factory=lambda *_args: None,
        save_cleanup_path=lambda _path: None,
        launch_patch=lambda _path: None,
    )


def test_update_dialog_renders_accumulated_markdown_in_scrollable_view() -> None:
    dialog = _dialog(
        UpdateInfo(
            UpdateKind.SETUP,
            _version("26.25.0.0"),
            "https://releases.example/setup.exe",
            (
                ReleaseNote(_version("26.25.0.0"), "- Latest change", "T"),
                ReleaseNote(_version("26.24.0.0"), "**Previous** change", "T"),
            ),
        )
    )

    browser = dialog._changelog_browser
    assert browser is not None
    assert dialog.findChild(QLabel, "version_badge").text() == "v26.25.0"
    assert browser.maximumHeight() == 300
    assert "v26.25.0" in browser.toPlainText()
    assert "v26.25.0.0" not in browser.toPlainText()
    assert "Latest change" in browser.toPlainText()
    assert "Previous change" in browser.toPlainText()


def test_update_dialog_keeps_legacy_compact_layout_without_changelog() -> None:
    dialog = _dialog(
        UpdateInfo(
            UpdateKind.SETUP,
            _version("26.25.0.0"),
            "https://releases.example/setup.exe",
        )
    )

    assert dialog._changelog_browser is None


def test_update_dialog_blocks_embedded_markdown_images() -> None:
    dialog = _dialog(
        UpdateInfo(
            UpdateKind.SETUP,
            _version("26.25.0.0"),
            "https://releases.example/setup.exe",
            (
                ReleaseNote(
                    _version("26.25.0.0"),
                    "![remote](https://example.com/image.png)",
                    "E",
                ),
            ),
        )
    )

    browser = dialog._changelog_browser
    assert browser is not None
    assert (
        browser.loadResource(
            QTextDocument.ResourceType.ImageResource,
            browser.source(),
        )
        is None
    )


def test_update_dialog_handles_unresolved_patch_install_scope() -> None:
    def fail_to_launch(_path: str) -> None:
        raise RuntimeError("installation scope is ambiguous")

    dialog = UpdateDialog(
        UpdateInfo(
            UpdateKind.PATCH,
            _version("26.25.0.0"),
            "https://releases.example/patch.exe",
        ),
        patch_downloader_factory=lambda *_args: None,
        save_cleanup_path=lambda _path: None,
        launch_patch=fail_to_launch,
    )
    dialog._patch_path = "patch.exe"
    dialog._btn_cancel.setEnabled(False)

    dialog._launch_patch_and_quit()

    assert dialog._status_label.text() == "Failed to launch installer."
    assert dialog._btn_cancel.isEnabled()
