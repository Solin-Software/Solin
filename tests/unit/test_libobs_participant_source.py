from __future__ import annotations

import types

import pytest

from solin.core.conference.media_ring import MediaRingWriter
from solin.core.scenes.engine import ParticipantIngressDescriptor
from solin.core.scenes.libobs_participant_source import LibobsParticipantVideo
from tests._qt import wait_until

W, H = 32, 16
FRAME = W * H * 3 // 2


class _FrameSource:
    def __init__(self):
        self.source = types.SimpleNamespace(audio_mixers=0x3F, muted=False)
        self.pushed = []
        self.cleared = 0
        self.released = False

    def push_i420(self, data, width, height):
        self.pushed.append((bytes(data[:1]), width, height))
        return True

    def clear(self):
        self.cleared += 1

    def release(self):
        self.released = True


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def ring():
    writer = MediaRingWriter(None, 4, FRAME, create=True)
    yield writer
    writer.close()
    writer.unlink()


def _record(ring, generation=1):
    return ParticipantIngressDescriptor("solin-participant", generation, ring.name).to_record()


def test_the_source_is_made_on_the_first_ingress_and_kept_silent(ring):
    sources = []
    video = LibobsParticipantVideo(object(), frame_source_factory=lambda _rt: sources.append(_FrameSource()) or sources[-1])
    assert video.source is None  # nothing until a meeting hands over a ring
    video.configure(_record(ring))
    (frame_source,) = sources
    assert video.source is frame_source.source
    assert frame_source.source.audio_mixers == 0 and frame_source.source.muted
    video.configure(None)
    video.configure(_record(ring, generation=2))
    assert len(sources) == 1  # kept: scene layers borrow it
    video.shutdown()
    assert frame_source.released


def test_newest_frames_are_pushed_and_a_stale_feed_is_cleared(ring):
    frame_source, clock = _FrameSource(), _Clock()
    video = LibobsParticipantVideo(object(), frame_source_factory=lambda _rt: frame_source,
                                   clock=clock)
    video.configure(_record(ring))
    for value in (1, 2, 3):
        ring.write(bytes([value]) * FRAME, meta=W << 16 | H)
    wait_until(lambda: frame_source.pushed, description="a frame pushed", timeout_ms=3000)
    assert frame_source.pushed[-1] == (b"\x03", W, H)  # newest only
    cleared = frame_source.cleared
    clock.now = 10.0
    wait_until(lambda: frame_source.cleared > cleared, description="cleared when stale",
               timeout_ms=3000)
    video.shutdown()


def test_invalid_descriptors_and_missing_rings_attach_nothing(ring):
    frame_source = _FrameSource()
    video = LibobsParticipantVideo(object(), frame_source_factory=lambda _rt: frame_source)
    video.configure({"channel_id": "x"})
    video.configure(ParticipantIngressDescriptor("p", 1, "psm_missing_ring").to_record())
    ring.write(b"\0" * FRAME, meta=W << 16 | H)
    assert frame_source.pushed == []
    video.shutdown()


def test_a_failing_factory_leaves_layers_with_their_stand_in(ring):
    def boom(_runtime):
        raise RuntimeError("no plugin")

    video = LibobsParticipantVideo(object(), frame_source_factory=boom)
    video.configure(_record(ring))
    assert video.source is None
    video.shutdown()
