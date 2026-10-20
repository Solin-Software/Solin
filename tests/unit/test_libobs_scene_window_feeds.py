from __future__ import annotations

import time

import pytest

from solin.core.scenes import libobs_sidecar
from solin.core.scenes.engine import SCENE_THUMBNAILS_CHANNEL, scene_window_channel
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine


class _Feed:
    def __init__(self) -> None:
        self.scene_ids: tuple[str, ...] = ()
        self.configured: list[tuple] = []
        self.shutdowns = 0

    def configure(self, descriptor, scene_ids, cell_width, cell_height) -> None:
        self.configured.append((descriptor, scene_ids, cell_width, cell_height))
        self.scene_ids = tuple(scene_ids) if descriptor is not None else ()

    def shutdown(self) -> None:
        self.shutdowns += 1


def _engine() -> tuple[LibobsSidecarEngine, _Feed, list[_Feed]]:
    engine = LibobsSidecarEngine()
    cards = _Feed()
    created: list[_Feed] = []

    def factory() -> _Feed:
        created.append(_Feed())
        return created[-1]

    engine._runtime_started = True
    engine._thumbnail_egress = cards  # type: ignore[assignment]
    engine._scene_render_factory = factory  # type: ignore[assignment]
    return engine, cards, created


def _request(payload: dict[str, object]) -> SceneIpcEnvelope:
    return SceneIpcEnvelope(
        message_type="set_thumbnail_egress",
        request_id="req-1",
        session_id="sess-1",
        process_generation="gen-1",
        sequence=0,
        document_revision=0,
        deadline_monotonic_ms=int(time.monotonic() * 1000) + 60_000,
        payload=payload,
    )


def _feed_payload(channel_id: str | None, scene_ids=("scene",), *, cleared: bool = False):
    payload: dict[str, object] = {
        "thumbnail_egress": None if cleared else {"transport": "shared_memory_bgra"},
        "scene_ids": [] if cleared else list(scene_ids),
        "cell_width": 0 if cleared else 640,
        "cell_height": 0 if cleared else 360,
    }
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return payload


def test_each_scene_window_gets_its_own_feed_beside_the_cards() -> None:
    engine, cards, created = _engine()
    channel = scene_window_channel("pulpit")

    response = engine._handle_set_thumbnail_egress(_request(_feed_payload(channel, ("pulpit",))))
    again = engine._handle_set_thumbnail_egress(_request(_feed_payload(channel, ("pulpit",))))

    assert response.payload["applied"] and again.payload["applied"]
    assert len(created) == 1  # reconfigured in place, not duplicated
    assert created[0].scene_ids == ("pulpit",)
    assert created[0].configured[-1][2:] == (640, 360)
    assert cards.configured == []  # the card atlas is untouched
    assert engine._scene_render_feeds() == (cards, created[0])


def test_closing_a_window_drops_only_its_feed() -> None:
    engine, cards, created = _engine()
    for scene_id in ("a", "b"):
        engine._handle_set_thumbnail_egress(
            _request(_feed_payload(scene_window_channel(scene_id), (scene_id,)))
        )

    engine._handle_set_thumbnail_egress(
        _request(_feed_payload(scene_window_channel("a"), cleared=True))
    )

    assert created[0].shutdowns == 1 and created[1].shutdowns == 0
    assert engine._scene_render_feeds() == (cards, created[1])


def test_requests_without_a_channel_still_address_the_card_atlas() -> None:
    engine, cards, created = _engine()

    engine._handle_set_thumbnail_egress(_request(_feed_payload(None, ("a", "b"))))
    engine._handle_set_thumbnail_egress(_request(_feed_payload(SCENE_THUMBNAILS_CHANNEL, cleared=True)))

    assert [call[1] for call in cards.configured] == [("a", "b"), ()]
    assert cards.shutdowns == 0  # the atlas outlives a closed panel
    assert created == []


@pytest.mark.parametrize("channel", ["program", "scene-window:", "scene-window:\n", 42])
def test_unknown_channels_are_refused(channel) -> None:
    engine, cards, created = _engine()

    response = engine._handle_set_thumbnail_egress(_request(_feed_payload(channel)))

    assert not response.payload["applied"]
    assert cards.configured == [] and created == []


def test_clearing_a_window_that_never_opened_creates_nothing() -> None:
    engine, _cards, created = _engine()

    response = engine._handle_set_thumbnail_egress(
        _request(_feed_payload(scene_window_channel("never"), cleared=True))
    )

    assert response.payload["applied"]
    assert created == []


def test_scene_window_feeds_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(libobs_sidecar, "_MAXIMUM_SCENE_WINDOW_FEEDS", 2)
    engine, _cards, created = _engine()

    results = [
        engine._handle_set_thumbnail_egress(
            _request(_feed_payload(scene_window_channel(str(index)), (str(index),)))
        ).payload["applied"]
        for index in range(3)
    ]

    assert results == [True, True, False]
    assert len(created) == 2


def test_shutdown_releases_every_scene_window_feed() -> None:
    engine, _cards, created = _engine()
    engine._handle_set_thumbnail_egress(
        _request(_feed_payload(scene_window_channel("a"), ("a",)))
    )

    for channel_id in tuple(engine._scene_window_feeds):
        engine._close_scene_window_feed(channel_id)

    assert created[0].shutdowns == 1
    assert engine._scene_window_feeds == {}
