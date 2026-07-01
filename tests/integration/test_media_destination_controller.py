from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog

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


class _MeetingsStub(QObject):
    destinationTargetsChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.requested_weeks = []
        self.session = _MeetingSession()

    def request_destination_week(self, monday, *, force=False):
        self.requested_weeks.append((monday, force))

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
    completed = Signal(int)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.added = []
        self.closed = False

    def placement_ref(self):
        return {"items": [], "sections": []}

    def add_items(self, items, *, list_id, insert_index):
        self.added.append((items, list_id, insert_index))
        self.completed.emit(len(items))

    def close(self):
        self.closed = True


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


def _controller(drive):
    meetings = _MeetingsStub()
    playlist_imports = _PlaylistImportsStub()
    notifications = _NotificationsStub()
    controller = MediaDestinationController(
        MediaDestinationContext(
            dialog_parent=None,
            playlist_widget=SimpleNamespace(get_playlist_names=lambda: [("saved", "Saved")]),
            meetings_widget=meetings,
            playlist_imports=playlist_imports,
            notifications=notifications,
            translate=lambda text, *_args: text,
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
    assert meetings.session.closed is True
    assert outcomes == [MediaDestinationOutcome(1, ("clip.mp4",), ("clip.mp4",))]
    assert notifications.events[0][0] == "success"
