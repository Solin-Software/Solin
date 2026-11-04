from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from solin.core.media.destinations import MediaDestinationOutcome
from solin.core.media.insertion import MediaInsertResult
from solin.widgets.meetings.destinations import MeetingDestinationSession
from solin.widgets.meetings.tree_controller import MeetingTreeController


class _ControllerStub(QObject):
    def __init__(self, added: int) -> None:
        super().__init__()
        self.added = added
        self.calls = []
        self.cleaned = False
        self.publication_calls = []
        self.publication_outcome = MediaDestinationOutcome(0)

    def placement_playlist_ref(self):
        return {"items": [], "sections": []}

    def add_external_media_items(self, items, *, list_id, insert_index):
        self.calls.append((items, list_id, insert_index))
        return MediaInsertResult(
            added_items=tuple(items[: self.added]),
        )

    def cleanup(self):
        self.cleaned = True

    def import_publications(self, paths, *, is_current):
        self.publication_calls.append((list(paths), is_current))
        return self.publication_outcome


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


def test_session_import_publications_forwards_live_guard_and_result() -> None:
    controller = _ControllerStub(0)
    controller.publication_outcome = MediaDestinationOutcome(
        2,
        ("one.png", "two.mp4"),
    )
    session = MeetingDestinationSession(controller, owned_controller=False)

    outcome = session.import_publications(["one.jwpub", "two.jwpub"])

    assert outcome == controller.publication_outcome
    assert len(controller.publication_calls) == 1
    paths, is_current = controller.publication_calls[0]
    assert paths == ["one.jwpub", "two.jwpub"]
    assert is_current()

    session.close()

    assert not is_current()
    assert session.import_publications(["ignored.jwpub"]) is None
    assert len(controller.publication_calls) == 1


def test_session_discards_result_when_origin_closes_during_selection() -> None:
    controller = _ControllerStub(0)
    session = MeetingDestinationSession(controller, owned_controller=False)

    def import_publications(paths, *, is_current):
        assert paths == ["source.jwpub"]
        assert is_current()
        session.close()
        return MediaDestinationOutcome(1, ("selected.png",))

    controller.import_publications = import_publications

    assert session.import_publications(["source.jwpub"]) is None


def test_controller_publication_import_uses_generic_selector_and_widget_parent() -> None:
    widget_parent = QWidget()
    object_parent = QObject(widget_parent)
    selector_calls = []

    class _Selector:
        def select(self, paths, **options):
            selector_calls.append((list(paths), options))
            options["insert"](
                [
                    {"title": "One", "url": "one.png"},
                    {"title": "Two", "url": "two.mp4"},
                ],
                "root",
                0,
                lambda _accepted: None,
            )

    def insert_nodes(_list_id, _index, nodes, *, on_completed, **_kwargs):
        on_completed(nodes)
        return True

    controller = SimpleNamespace(
        _tree_session=SimpleNamespace(owner_id="meeting:test", generation=1),
        _tree_key="meeting-key",
        _language_code="T",
        _folder_mutation_pending=False,
        _jwpub_origin_generation=0,
        _jwpub_selection_factory=_Selector(),
        _media_tree_runtime=SimpleNamespace(operations=object()),
        _jwpub_image_dir=lambda: "images",
        _node_from_playlist_item=lambda item, _title: {
            "media_ref": {"file_path": item["url"]},
        },
        _insert_nodes=insert_nodes,
        placement_playlist_ref=lambda: {"items": [], "sections": []},
        parent=lambda: object_parent,
    )
    controller._import_jwpubs = lambda paths, **kwargs: MeetingTreeController._import_jwpubs(
        controller,
        paths,
        **kwargs,
    )

    outcome = MeetingTreeController.import_publications(
        controller,
        ["source.jwpub"],
        is_current=lambda: True,
    )

    assert outcome == MediaDestinationOutcome(2, ("one.png", "two.mp4"))
    assert len(selector_calls) == 1
    paths, options = selector_calls[0]
    assert paths == ["source.jwpub"]
    assert options["parent"] is widget_parent
    assert options["list_id"] is None
    assert options["public_talk"] is False
    assert options["auto_target"] is None

    object_parent.deleteLater()
    widget_parent.deleteLater()
