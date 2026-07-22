from PySide6.QtCore import QCoreApplication

from solin.ui.qml.media_tree.probe_queue import ProbeRequestQueue


_APP = QCoreApplication.instance() or QCoreApplication([])


def test_probe_request_queue_coalesces_and_bounds_each_qt_batch() -> None:
    consumed: list[str] = []
    queue = ProbeRequestQueue(consumed.append, batch_size=3)
    for node_id in ("a", "b", "c", "d", "b"):
        queue.enqueue(node_id)

    queue.drain_now()

    assert consumed == ["a", "b", "c"]
    assert queue.pending_count == 1
    queue.drain_now()
    assert consumed == ["a", "b", "c", "d"]
    assert queue._timer.isActive() is False


def test_probe_request_queue_can_prioritize_a_late_thumbnail_refresh() -> None:
    consumed: list[str] = []
    queue = ProbeRequestQueue(consumed.append, batch_size=2)
    for node_id in ("a", "b", "c"):
        queue.enqueue(node_id)

    queue.enqueue("c", priority=True)
    queue.drain_now()

    assert consumed == ["c", "a"]


def test_probe_request_queue_discards_obsolete_work_and_stops_cleanly() -> None:
    consumed: list[str] = []
    queue = ProbeRequestQueue(consumed.append, batch_size=3)
    queue.enqueue("obsolete")
    queue.enqueue("keep")
    queue.discard("obsolete")

    queue.drain_now()
    queue.close()
    queue.enqueue("after-close")
    queue.drain_now()

    assert consumed == ["keep"]
    assert queue.pending_count == 0
