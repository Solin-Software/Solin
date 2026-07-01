from PySide6.QtCore import QObject

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
        return self.added

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

    assert completed == [1]
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


def test_owned_session_cleans_up_headless_controller_once() -> None:
    controller = _ControllerStub(1)
    session = MeetingDestinationSession(controller, owned_controller=True)

    session.close()
    session.close()

    assert controller.cleaned is True
