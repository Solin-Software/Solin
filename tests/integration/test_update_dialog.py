from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QObject, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from dataclasses import replace
from contextlib import nullcontext
import gc
from hashlib import sha256
from threading import Event
from types import SimpleNamespace
import weakref
import pytest
from scripts.release_contributors import Change, ContributionRange, Credits, RelatedIssue
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    LocalizedReleaseNotes, REQUIRED_ASSETS, ReleaseAsset, ReleaseManifest, asset_filename,
)
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import ReleaseNote, UpdateAction, UpdateInfo, evaluate_update
from solin.ui.dialogs.update import UpdateDialog
from solin.core.remote import update_download
from tests._qt import wait_until

_APP = QApplication.instance() or QApplication([])


def test_cancelled_download_survives_its_owner_until_thread_exits(monkeypatch, tmp_path):
    entered, release = Event(), Event()

    class Transfer(update_download._Transfer):
        @Slot()
        def run(self):
            entered.set()
            release.wait(5)

    monkeypatch.setattr(update_download, "_Transfer", Transfer)
    instance = dialog()
    worker = update_download.UpdateDownloadWorker(instance._info, instance, directory=tmp_path)
    reference = weakref.ref(worker)
    destroyed = []
    worker.destroyed.connect(lambda: destroyed.append(True))
    try:
        worker.start()
        wait_until(entered.is_set, description="transfer start")
        worker.abort()
        instance.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        del worker
        gc.collect()
        assert reference() is not None
        assert destroyed == []
    finally:
        release.set()
        wait_until(
            lambda: reference() is None or reference()._thread is None,
            description="cancelled download shutdown",
        )
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == [True]


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("valid_package", [False, True])
def test_download_delivers_result_after_thread_stops(monkeypatch, tmp_path, cancel, valid_package):
    emitted, release = Event(), Event()

    class Transfer(update_download._Transfer):
        @Slot()
        def run(self):
            super().run()
            emitted.set()
            release.wait(5)

    content = b"abc" if valid_package else b"abd"
    response = SimpleNamespace(status_code=200, content_length=3, iter_bytes=lambda: iter([content]))
    monkeypatch.setattr(update_download, "stream_get", lambda *args, **kwargs: nullcontext(response))
    monkeypatch.setattr(update_download, "_Transfer", Transfer)
    launches = []
    instance = dialog(launch=launches.append)
    instance._info = replace(
        instance._info, asset=replace(instance._info.asset, sha256=sha256(b"abc").hexdigest()),
    )
    worker = update_download.UpdateDownloadWorker(instance._info, instance, directory=tmp_path)
    failures = []
    worker.failed.connect(failures.append)
    instance._downloader_factory = lambda *args: worker
    try:
        instance._on_action()
        wait_until(emitted.is_set, description="transfer success")
        QTest.qWait(50)
        assert launches == []
        assert failures == []
        if cancel:
            assert instance.invalidate()
        release.set()
        wait_until(lambda: worker._thread is None, description="download thread shutdown")
        assert launches == ([str(tmp_path / "Solin.exe")] if valid_package and not cancel else [])
        assert bool(failures) is (not valid_package and not cancel)
    finally:
        release.set()
        if worker._thread is not None:
            wait_until(lambda: worker._thread is None, description="download thread shutdown")
        instance.deleteLater()


def dialog(action=UpdateAction.INSTALL, launch=lambda path: None, markdown="- Changes"):
    version = ReleaseVersion.parse("26.32.0")
    info = UpdateInfo(
        action,
        version,
        ReleaseAsset("windows", "x86_64", "installer", "Solin.exe", 3, "a" * 64, "10"),
        "https://github.com/file",
        (ReleaseNote(version, markdown, "en"),),
    )
    return UpdateDialog(info, downloader_factory=lambda *args: None, launch_installer=launch)


def test_download_completion_starts_confirmed_installation():
    launches = []
    instance = dialog(launch=launches.append)
    instance._on_download_done("Solin.exe")
    assert launches == ["Solin.exe"]
    assert instance._btn_action.text() == "Install and restart"
    assert not instance._btn_action.isEnabled()
    assert instance._btn_cancel.isEnabled()
    instance._on_action()
    assert launches == ["Solin.exe"]


def test_assisted_package_download_still_waits_for_user_action():
    launches = []
    instance = dialog(UpdateAction.REVEAL, launch=launches.append)
    instance._on_download_done("Solin.exe")
    assert launches == []
    assert instance._btn_action.text() == "Show in folder"
    assert instance._btn_action.isEnabled()


def test_changelog_link_opens_externally_without_replacing_notes(monkeypatch, request):
    url = "https://github.com/Solin-Software/Solin/pull/1"
    instance = dialog(markdown=f"[PR #1]({url})\n\nStill here")
    request.addfinalizer(instance.deleteLater)
    browser = instance._changelog_browser
    assert browser is not None
    instance.show()
    _APP.processEvents()

    point = next(
        (
            QPoint(x, y)
            for y in range(browser.viewport().height())
            for x in range(browser.viewport().width())
            if browser.anchorAt(QPoint(x, y)) == url
        ),
        None,
    )
    assert point is not None
    before = browser.toPlainText()
    opened = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda target: opened.append(target.toString()) or True,
    )

    QTest.mouseClick(browser.viewport(), Qt.MouseButton.LeftButton, pos=point)
    _APP.processEvents()

    assert opened == [url]
    assert browser.toPlainText() == before
    assert browser.source().isEmpty()


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
    launches = []

    def fail(path):
        launches.append(path)
        raise RuntimeError("scope unavailable")

    instance = dialog(launch=fail)
    stopped = []
    instance.installation_stopped.connect(lambda: stopped.append(True))
    instance._on_download_done("Solin.exe")
    assert instance._btn_action.isEnabled()
    assert instance._status_label.text() == "Failed to launch installer."
    assert stopped == [True]
    instance._on_action()
    assert launches == ["Solin.exe", "Solin.exe"]
    assert stopped == [True, True]
    assert instance.invalidate()


def test_failed_download_allows_retry():
    instance = dialog()
    instance._on_download_failed("disk full")
    assert instance._btn_action.isEnabled()
    assert instance._btn_action.text() == "Try again"


def test_beta_and_return_offers_identify_the_actual_destination(request):
    from PySide6.QtWidgets import QLabel

    template = dialog()
    request.addfinalizer(template.deleteLater)
    beta = UpdateDialog(
        replace(template._info, version=ReleaseVersion.parse("26.33.0b1")),
        downloader_factory=lambda *args: None, launch_installer=lambda path: None,
    )
    request.addfinalizer(beta.deleteLater)
    assert beta.findChild(QLabel, "beta_badge") is not None
    assert beta.findChild(QLabel, "release_warning") is not None

    stable = UpdateDialog(
        replace(template._info, is_downgrade=True, current_version=ReleaseVersion.parse("26.33.0b1")),
        downloader_factory=lambda *args: None, launch_installer=lambda path: None,
    )
    request.addfinalizer(stable.deleteLater)
    assert stable.findChild(QLabel, "beta_badge") is None
    assert stable.findChild(QLabel, "release_warning") is not None
    text = " ".join(label.text() for label in stable.findChildren(QLabel))
    assert "26.33.0-beta.1" in text
    assert "26.32.0" in text


def test_invalidating_offer_aborts_download_and_rejects_late_completion(request):
    class Download(QObject):
        progress = Signal(int)
        finished = Signal(str)
        failed = Signal(str)

        def start(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    launches = []
    instance = dialog(launch=launches.append)
    request.addfinalizer(instance.deleteLater)
    download = Download(instance)
    instance._downloader_factory = lambda *args: download
    instance._on_action()
    assert instance.invalidate()
    assert download.aborted
    download.finished.emit("Solin.exe")
    instance._on_action()
    assert instance.downloaded_path == ""
    assert launches == []


def test_confirmed_installation_is_not_invalidated_or_launched_twice(request):
    launches = []
    instance = dialog(launch=launches.append)
    request.addfinalizer(instance.deleteLater)
    instance._on_download_done("Solin.exe")
    assert not instance.invalidate()
    instance._on_action()
    assert launches == ["Solin.exe"]
