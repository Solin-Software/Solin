from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication
from scripts.release_contributors import Change, ContributionRange, Credits, RelatedIssue
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    LocalizedReleaseNotes, REQUIRED_ASSETS, ReleaseAsset, ReleaseManifest, asset_filename,
)
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import ReleaseNote, UpdateAction, UpdateInfo, evaluate_update
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


def test_dialog_displays_automatic_beta_notes_from_the_existing_manifest_contract():
    version = ReleaseVersion.parse("26.32.0b6")
    head = "a" * 40
    repository = "https://github.com/Solin-Software/Solin"
    credits = Credits(ContributionRange("b" * 40, head, (head,)), (
        Change("fix(player): restore playback", "PR #7", f"{repository}/pull/7", ("alice",),
               (RelatedIssue("#3", f"{repository}/issues/3"),), ("bug",)),
    ), ())
    manifest = ReleaseManifest(
        version, version.tag, version.channel, head,
        tuple(ReleaseAsset(platform, architecture, kind, asset_filename(version, platform, architecture, kind),
                           3, "c" * 64, minimum_os)
              for (platform, architecture, kind), (_, minimum_os) in REQUIRED_ASSETS.items()),
        (LocalizedReleaseNotes(version, {"en": credits.content_markdown().strip()}),),
    )
    parsed = ReleaseManifest.parse(manifest.to_dict())
    assert parsed is not None
    info = evaluate_update(
        [parsed], current_version="26.32.0b5", channel=UpdateChannel.BETA,
        platform="windows", architecture="x86_64", os_version="10.0.22631", language="pt_BR",
    )
    assert info is not None
    instance = UpdateDialog(info, downloader_factory=lambda *args: None, launch_installer=lambda path: None)
    try:
        browser = instance._changelog_browser
        text = browser.toPlainText()
        assert all(part in text for part in ("Related pull requests and issues", "Bug fixes", "PR #7", "#3"))
        assert f'{repository}/pull/7' in browser.toHtml()
        assert f'{repository}/issues/3' in browser.toHtml()
    finally:
        instance.close()
        instance.deleteLater()


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
