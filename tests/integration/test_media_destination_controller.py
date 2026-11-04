from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from solin.controllers.media_destination_controller import (
    MediaDestinationContext,
    MediaDestinationController,
)
from solin.core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationOutcome,
    MediaDestinationRequest,
    PlaylistDestinationTarget,
    PreparedMediaBatch,
)
from solin.core.media.insertion import MediaInsertResult
from solin.ui.qml.media_destination import MediaDestinationBridge, MediaDestinationDialog


class _MeetingsStub(QObject):
    destinationTargetsChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.requested_weeks = []
        self.prepared_meetings = []
        self.session = _MeetingSession()

    def request_destination_week(self, monday, *, force=False):
        self.requested_weeks.append((monday, force))

    def prepare_destination_meeting(self, monday, pub_type, *, force=False):
        self.prepared_meetings.append((monday.isoformat(), pub_type, force))

    def destination_targets(self, _monday):
        return [
            {
                "pub_type": "mwb",
                "title": "Midweek",
                "status": "ready",
                "available": True,
            },
            {
                "pub_type": "wt",
                "title": "Weekend",
                "status": "loading",
                "available": False,
            },
        ]

    def open_destination_session(self, _monday, pub_type):
        return self.session if pub_type == "mwb" else None


class _MeetingSession(QObject):
    completed = Signal(object)
    failed = Signal(str)
    closed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.added = []
        self.is_closed = False

    def placement_ref(self):
        return {"items": [], "sections": []}

    def add_items(self, items, *, list_id, insert_index):
        self.added.append((items, list_id, insert_index))
        self.completed.emit(MediaInsertResult(added_items=tuple(items)))

    def close(self):
        if not self.is_closed:
            self.is_closed = True
            self.closed.emit(self)


class _PlaylistImportsStub:
    def __init__(self) -> None:
        self.calls = []

    def add_items_to_playlist_target(self, target, items, source_name):
        self.calls.append((target, items, source_name))
        return MediaDestinationOutcome(
            len(items),
            tuple(str(item["url"]) for item in items),
        )

    def prepare_destination_assets(self, assets, completed):
        completed(
            PreparedMediaBatch(
                tuple(asset.item for asset in assets if asset.item is not None),
                tuple(asset.source_id for asset in assets),
            )
        )


class _NotificationsStub:
    def __init__(self) -> None:
        self.events = []

    def success(self, message, **_kwargs):
        self.events.append(("success", message))

    def error(self, message, **_kwargs):
        self.events.append(("error", message))

    def warning(self, message, **_kwargs):
        self.events.append(("warning", message))


class _DialogStub:
    def __init__(self, bridge, _parent, drive) -> None:
        self._bridge = bridge
        self._drive = drive

    @property
    def selection(self):
        return self._bridge.result

    def exec(self):
        self._drive(self._bridge)
        return (
            QDialog.DialogCode.Accepted
            if self._bridge.result is not None
            else QDialog.DialogCode.Rejected
        )


def _controller(drive, *, playlist_ref=None):
    meetings = _MeetingsStub()
    playlist_imports = _PlaylistImportsStub()
    notifications = _NotificationsStub()
    controller = MediaDestinationController(
        MediaDestinationContext(
            dialog_parent=None,
            playlist_widget=SimpleNamespace(
                get_playlist_names=lambda: [("saved", "Saved")],
                playlist_placement_ref=lambda _playlist_id: (
                    playlist_ref
                    if playlist_ref is not None
                    else {"items": [], "sections": []}
                ),
            ),
            meetings_widget=meetings,
            playlist_imports=playlist_imports,
            notifications=notifications,
            dialog_factory=lambda bridge, parent: _DialogStub(bridge, parent, drive),
        )
    )
    return controller, meetings, playlist_imports, notifications


def _request(*, can_play=False):
    return MediaDestinationRequest(
        title="Clip",
        assets=(
            MediaDestinationAsset(
                title="Clip",
                source_id="clip.mp4",
                item={"title": "Clip", "url": "clip.mp4", "type": "video"},
            ),
        ),
        can_play=can_play,
    )


def test_play_route_does_not_mutate_any_destination() -> None:
    controller, _meetings, playlist_imports, _notifications = _controller(
        lambda bridge: bridge.choosePlay()
    )
    played = []

    controller.route(_request(can_play=True), play=lambda: played.append(True))

    assert played == [True]
    assert playlist_imports.calls == []


def test_playlist_route_returns_durable_references_to_source() -> None:
    def drive(bridge):
        bridge.showPlaylists()
        bridge.choosePlaylist("saved", "Saved")

    controller, _meetings, playlist_imports, _notifications = _controller(drive)
    outcomes = []

    controller.route(_request(), completed=outcomes.append)

    assert playlist_imports.calls[0][0] == PlaylistDestinationTarget("saved", "Saved")
    assert outcomes == [MediaDestinationOutcome(1, ("clip.mp4",), ("clip.mp4",))]


def test_playlist_route_requests_placement_for_a_structured_playlist() -> None:
    def drive(bridge):
        bridge.showPlaylists()
        bridge.choosePlaylist("saved", "Saved")
        assert bridge.step == "placement"
        bridge.confirmPlacement("top")

    controller, _meetings, playlist_imports, _notifications = _controller(
        drive,
        playlist_ref={
            "items": [{} for _ in range(14)],
            "sections": [{"id": "talk", "name": "Talk"}],
        },
    )

    controller.route(_request())

    assert playlist_imports.calls[0][0] == PlaylistDestinationTarget(
        "saved",
        "Saved",
        list_id="root",
        insert_index=0,
    )


def test_preparation_stays_in_same_wizard_before_playlist_selection() -> None:
    def drive(bridge):
        bridge.requestAdd()
        assert bridge.step == "destination"
        bridge.showPlaylists()
        bridge.choosePlaylist("saved", "Saved")

    controller, _meetings, playlist_imports, _notifications = _controller(drive)

    controller.route(
        MediaDestinationRequest(title="Remote", can_play=True),
        prepare=lambda succeed, _fail: succeed(_request()),
    )

    assert len(playlist_imports.calls) == 1


def test_meeting_route_loads_week_and_inserts_through_session() -> None:
    def drive(bridge):
        bridge.showMeetings()
        bridge.chooseMeeting("mwb")

    controller, meetings, _playlist_imports, notifications = _controller(drive)
    outcomes = []

    controller.route(_request(), completed=outcomes.append)

    assert len(meetings.requested_weeks) == 1
    assert meetings.session.added[0][1:] == ("root", 2**31 - 1)
    assert meetings.session.is_closed is True
    assert outcomes == [MediaDestinationOutcome(1, ("clip.mp4",), ("clip.mp4",))]
    assert notifications.events[0][0] == "success"


def test_expired_meeting_rejects_assets_prepared_after_session_closure() -> None:
    def drive(bridge):
        bridge.showMeetings()
        bridge.chooseMeeting("mwb")

    controller, meetings, imports, notifications = _controller(drive)
    pending = []
    imports.prepare_destination_assets = lambda _assets, callback: pending.append(callback)
    outcomes = []
    controller.route(_request(), completed=outcomes.append)

    meetings.session.close()
    pending[0](PreparedMediaBatch(
        ({"url": "clip.mp4", "type": "video"},), ("clip.mp4",),
    ))

    assert not meetings.session.added
    assert outcomes == [MediaDestinationOutcome(0, destination_accepted=False)]
    assert notifications.events[0][0] == "error"


def test_meeting_duplicate_is_reported_as_warning_not_failure() -> None:
    def drive(bridge):
        bridge.showMeetings()
        bridge.chooseMeeting("mwb")

    controller, meetings, _playlist_imports, notifications = _controller(drive)
    outcomes = []

    def reject_duplicate(items, **_kwargs):
        meetings.session.completed.emit(
            MediaInsertResult(duplicate_items=tuple(items))
        )

    meetings.session.add_items = reject_duplicate
    controller.route(_request(), completed=outcomes.append)

    assert meetings.session.is_closed is True
    assert outcomes == [MediaDestinationOutcome(0, (), ("clip.mp4",))]
    assert notifications.events == [("warning", "“Clip” is already added.")]


@pytest.mark.parametrize(("status", "force"), [("download", False), ("error", True)])
@pytest.mark.parametrize("pub_type", ["mwb", "wt"])
def test_meeting_preparation_forwards_the_selected_retry_policy(status, force, pub_type) -> None:
    selected_monday = []

    def drive(bridge):
        bridge.showMeetings()
        selected_monday.append(bridge.weekMonday)
        bridge.update_meeting_targets(bridge.weekMonday, [
            {"pub_type": pub_type, "available": False, "status": status},
        ])
        bridge.downloadMeeting(pub_type)

    controller, meetings, _imports, _notifications = _controller(drive)
    controller.route(_request())

    assert meetings.prepared_meetings == [(selected_monday[0], pub_type, force)]
    assert not any(force for _monday, force in meetings.requested_weeks)


@pytest.mark.usefixtures("all_controls_tab_focus")
@pytest.mark.parametrize("status", ["download", "error"])
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space])
def test_meeting_preparation_action_is_keyboard_accessible(status, key) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    bridge = MediaDestinationBridge(
        media_title="Synthetic", item_count=1, can_play=False, playlists=(),
    )
    bridge.showMeetings()
    bridge.update_meeting_targets(bridge.weekMonday, [
        {"pub_type": "mwb", "title": "Midweek", "available": False, "status": status},
        {"pub_type": "wt", "title": "Weekend", "available": True, "status": "ready"},
    ])
    requested = []
    bridge.meetingDownloadRequested.connect(lambda *args: requested.append(args))
    dialog = MediaDestinationDialog(bridge)
    try:
        dialog.show()
        dialog.activateWindow()
        app.processEvents()
        root = dialog.qml_widget.rootObject()
        assert root is not None

        def items(item):
            yield item
            for child in item.childItems():
                yield from items(child)

        actions = [
            item for item in items(root)
            if item.objectName() == "meetingPreparationAction" and item.isVisible()
        ]
        assert len(actions) == 1
        action = actions[0]
        accessible = QAccessible.queryAccessibleInterface(action)
        assert accessible is not None
        assert accessible.role() == QAccessible.Role.Button
        assert accessible.text(QAccessible.Text.Name) == action.property("text")

        dialog.qml_widget.setFocus()
        root.forceActiveFocus()
        QTest.keyClick(dialog.qml_widget, Qt.Key.Key_Tab)
        assert action.hasActiveFocus()
        QTest.keyClick(dialog.qml_widget, key)

        assert requested == [("mwb", bridge.weekMonday, status == "error")]
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
