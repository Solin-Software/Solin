from __future__ import annotations

import struct
import types

import pytest

from solin.core.conference.media_ring import MediaRingReader
from solin.core.scenes.engine import MEETING_AUDIO_MIX_INDEX, MeetingEgressDescriptor
from solin.core.scenes.libobs_meeting_egress import LibobsMeetingEgress, pack_i420

WIDTH, HEIGHT = 32, 16
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2


@pytest.fixture
def rings():
    video = MediaRingReader(None, slot_count=4, payload_size=FRAME_BYTES, create=True)
    audio = MediaRingReader(None, slot_count=8, payload_size=4096, create=True)
    yield video, audio
    for ring in (video, audio):
        ring.close()
        ring.unlink()


@pytest.fixture
def fake_ob():
    calls = types.SimpleNamespace(video=[], audio=[], removed_video=[], removed_audio=[])
    ob = types.SimpleNamespace(
        VideoFormat=types.SimpleNamespace(I420=1),
        add_raw_video_callback=lambda cb, **kw: calls.video.append((cb, kw)) or cb,
        add_raw_audio_callback=lambda cb, **kw: calls.audio.append((cb, kw)) or cb,
        remove_raw_video_callback=calls.removed_video.append,
        remove_raw_audio_callback=lambda cb, mix_idx: calls.removed_audio.append((cb, mix_idx)),
    )
    return ob, calls


def _record(video, audio, **overrides):
    return {
        **MeetingEgressDescriptor(
            channel_id="solin-meeting",
            generation=1,
            video_ring=video.name,
            audio_ring=audio.name,
            width=WIDTH,
            height=HEIGHT,
            fps=30,
        ).to_record(),
        **overrides,
    }


def _planes(fill_y=1, fill_u=2, fill_v=3):
    return [
        bytes([fill_y]) * (WIDTH * HEIGHT),
        bytes([fill_u]) * (WIDTH // 2 * HEIGHT // 2),
        bytes([fill_v]) * (WIDTH // 2 * HEIGHT // 2),
    ]


def test_configure_registers_i420_video_and_meeting_track_audio(rings, fake_ob):
    video, audio = rings
    ob, calls = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio))
    assert egress.active
    (_, video_kwargs), = calls.video
    assert video_kwargs == {"format": 1, "width": WIDTH, "height": HEIGHT}
    (_, audio_kwargs), = calls.audio
    assert audio_kwargs == {"mix_idx": MEETING_AUDIO_MIX_INDEX, "channels": 2}
    egress.shutdown()
    assert len(calls.removed_video) == 1
    assert calls.removed_audio[0][1] == MEETING_AUDIO_MIX_INDEX
    assert not egress.active


def test_video_frames_reach_the_ring_packed(rings, fake_ob):
    video, audio = rings
    ob, _ = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio))
    egress._on_video(_planes(), [WIDTH, WIDTH // 2, WIDTH // 2], WIDTH, HEIGHT, 1, 123)
    item = video.read_next()
    assert item.payload == b"".join(_planes())
    assert item.timestamp_ns == 123
    assert item.meta == WIDTH << 16 | HEIGHT
    assert egress.video_frames == 1
    egress.shutdown()


def test_audio_chunks_are_planar_float32_with_frame_count(rings, fake_ob):
    video, audio = rings
    ob, _ = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio))
    left = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
    right = struct.pack("<4f", -0.1, -0.2, -0.3, -0.4)
    egress._on_audio(MEETING_AUDIO_MIX_INDEX, [left, right], 4, 999)
    egress._on_audio(MEETING_AUDIO_MIX_INDEX, [left, b""], 4, 1000)  # mono → duplicated
    stereo, mono = audio.read_next(), audio.read_next()
    assert stereo.payload == left + right and stereo.meta == 4 | 2 << 16
    assert mono.payload == left + left
    egress.shutdown()


def test_same_descriptor_is_a_no_op_and_a_new_generation_reattaches(rings, fake_ob):
    video, audio = rings
    ob, calls = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio))
    egress.configure(_record(video, audio))
    assert len(calls.video) == 1
    egress.configure(_record(video, audio, generation=2))
    assert len(calls.video) == 2 and len(calls.removed_video) == 1
    egress.configure(None)
    assert not egress.active and len(calls.removed_video) == 2


def test_invalid_descriptors_do_not_attach(rings, fake_ob):
    video, audio = rings
    ob, calls = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio, width=31))  # odd width is not I420
    egress.configure({"unexpected": True})
    egress.configure(_record(video, audio, video_ring="psm_does_not_exist_x"))
    assert not egress.active and calls.video == []


def test_callbacks_after_teardown_write_nothing(rings, fake_ob):
    video, audio = rings
    ob, _ = fake_ob
    egress = LibobsMeetingEgress(types.SimpleNamespace(ob=ob))
    egress.configure(_record(video, audio))
    egress.shutdown()
    egress._on_video(_planes(), [WIDTH, WIDTH // 2, WIDTH // 2], WIDTH, HEIGHT, 1, 1)
    assert video.read_next() is None


def test_pack_i420_removes_row_padding():
    width, height, pad = 4, 2, 4
    y = bytes([1, 1, 1, 1, 9, 9, 9, 9, 1, 1, 1, 1, 9, 9, 9, 9])  # stride 8, 4 visible
    u = bytes([2, 2, 9, 9])  # stride 4, 2 visible, 1 row
    v = bytes([3, 3, 9, 9])
    packed = pack_i420([y, u, v], [width + pad, 4, 4], width, height)
    assert packed == bytes([1] * 8 + [2, 2] + [3, 3])


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 15},
        {"height": 4000},
        {"fps": 0},
        {"fps": 61},
        {"audio_mix": 6},
        {"video_ring": ""},
        {"generation": -1},
    ],
)
def test_meeting_descriptor_rejects_invalid_values(overrides):
    values = {
        "channel_id": "solin-meeting", "generation": 1, "video_ring": "v", "audio_ring": "a",
        "width": 640, "height": 360, "fps": 30, **overrides,
    }
    with pytest.raises(ValueError):
        MeetingEgressDescriptor(**values)
