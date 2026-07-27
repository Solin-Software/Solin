from PySide6.QtCore import QCoreApplication, QEvent, QObject
from shiboken6 import isValid

from solin.core.media.insertion import MediaInsertResult
from solin.widgets.meetings.destinations import MeetingDestinationSession


class _ControllerStub(QObject):
    def __init__(self, added: int) -> None:
        super().__init__()
        self.added = added
        self.calls = []
        self.cleaned = False

    def placement_playlist_ref(self):
        return {"items": [], "sections": []}

    def add_external_media_items(self, items, *, list_id, insert_index):
        self.calls.append((items, list_id, insert_index))
        return MediaInsertResult(
            added_items=tuple(items[: self.added]),
        )

    def cleanup(self):
        self.cleaned = True


def test_session_reports_completion_only_after_controller_accepts_insert() -> None:
    controller = _ControllerStub(1)
    session = MeetingDestinationSession(controller, owned_controller=False)
    completed = []
    failed = []
    session.completed.connect(completed.append)
    session.failed.connect(failed.append)

    session.add_items(
        [{"title": "Clip", "url": "clip.mp4"}],
        list_id="root",
        insert_index=0,
    )

    assert len(completed) == 1
    assert completed[0].added_count == 1
    assert failed == []


def test_session_reports_failed_or_rejected_persistence() -> None:
    controller = _ControllerStub(0)
    session = MeetingDestinationSession(controller, owned_controller=False)
    completed = []
    failed = []
    session.completed.connect(completed.append)
    session.failed.connect(failed.append)

    session.add_items(
        [{"title": "Clip", "url": "clip.mp4"}],
        list_id="root",
        insert_index=0,
    )

    assert completed == []
    assert failed == ["No media could be added."]


def test_session_reports_duplicate_separately() -> None:
    controller = _ControllerStub(0)
    controller.add_external_media_items = lambda items, **_kwargs: MediaInsertResult(
        duplicate_items=tuple(items)
    )
    session = MeetingDestinationSession(controller, owned_controller=False)
    completed = []
    session.completed.connect(completed.append)

    session.add_items(
        [{"title": "Clip", "url": "clip.mp4"}],
        list_id="root",
        insert_index=0,
    )

    assert len(completed) == 1
    assert completed[0].duplicate_count == 1


def test_owned_session_cleans_up_headless_controller_once() -> None:
    controller = _ControllerStub(1)
    session = MeetingDestinationSession(controller, owned_controller=True)

    session.close()
    session.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert controller.cleaned is True
    assert not isValid(controller)
    assert not isValid(session)
