from dataclasses import replace

import pytest

from solin.core.scenes.idle import IdleScreenState


def test_idle_state_round_trip_and_strict_fields() -> None:
    state = IdleScreenState(3, "loop.mp4", "yeartext.png", 2)
    assert IdleScreenState.from_record(state.to_record()) == state
    with pytest.raises(ValueError):
        IdleScreenState.from_record({**state.to_record(), "muted": False})


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"revision": -1}, {"yeartext_revision": 2**64},
    {"media_path": "https://example.test/video.mp4"},
    {"media_path": "video\x00.mp4"}, {"yeartext_image_path": "x" * 4097},
])
def test_idle_state_rejects_unsafe_or_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        replace(IdleScreenState(), **changes)
