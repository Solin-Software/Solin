from __future__ import annotations

from solin.controllers import scene_thumbnail_egress as thumbnail_egress


class _Reader:
    _next_id = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self)._next_id += 1
        self.name = f"thumbnail-test-{self._next_id}"

    def read_latest(self):
        return None

    def close(self) -> None:
        pass

    def unlink(self) -> None:
        pass


def test_thumbnail_worker_exists_only_while_a_channel_is_active(monkeypatch) -> None:
    monkeypatch.setattr(thumbnail_egress, "SharedFrameChannelReader", _Reader)
    controller = thumbnail_egress.SceneThumbnailEgressController()

    assert controller._worker is None
    assert controller.reconfigure(("scene-a",), 160, 90)
    first_worker = controller._worker
    assert first_worker is not None and first_worker.is_alive()

    controller.stop()

    assert controller._worker is None
    assert not first_worker.is_alive()

    assert controller.reconfigure(("scene-a",), 160, 90)
    second_worker = controller._worker
    assert second_worker is not None and second_worker is not first_worker
    assert second_worker.is_alive()

    controller.close()

    assert controller._worker is None
    assert not second_worker.is_alive()


def test_closed_thumbnail_egress_cannot_restart_its_worker(monkeypatch) -> None:
    monkeypatch.setattr(thumbnail_egress, "SharedFrameChannelReader", _Reader)
    controller = thumbnail_egress.SceneThumbnailEgressController()

    controller.close()

    assert controller.reconfigure(("scene-a",), 160, 90) is False
    assert controller._worker is None
