"""Remote part-holder video slots: the shared SDP/port contract and the scene source kind."""

from __future__ import annotations

import pytest

from solin.core.hosted_meeting import participant_video
from solin.core.scenes.model import (
    REMOTE_PARTICIPANT_SLOTS,
    RemoteParticipantConfig,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
)


def test_slot_ports_and_bridge_argument():
    assert participant_video.SLOT_COUNT == REMOTE_PARTICIPANT_SLOTS == len(participant_video.SLOT_PORTS)
    assert all(port % 2 == 0 for port in participant_video.SLOT_PORTS)  # RTCP takes port + 1
    assert participant_video.bridge_ports_argument() == "47110,47112"
    assert participant_video.slot_port(2) == 47112
    for bad in (0, 3, True, "1"):
        assert not participant_video.valid_slot(bad)
    with pytest.raises(ValueError):
        participant_video.slot_port(3)


def test_sdp_describes_the_bridge_stream():
    sdp = participant_video.sdp_text(1)
    # The bridge rewrites every slot to payload type 96 on loopback.
    assert "c=IN IP4 127.0.0.1" in sdp
    assert "m=video 47110 RTP/AVP 96" in sdp
    assert "a=rtpmap:96 H264/90000" in sdp
    assert "packetization-mode=1" in sdp


def test_sdp_file_is_written_once_and_repaired(tmp_path):
    path = participant_video.ensure_sdp_file(2, tmp_path)
    assert path == tmp_path / "slot2.sdp"
    assert path.read_text(encoding="utf-8") == participant_video.sdp_text(2)
    stamp = path.stat().st_mtime_ns
    assert participant_video.ensure_sdp_file(2, tmp_path) == path
    assert path.stat().st_mtime_ns == stamp
    path.write_text("stale", encoding="utf-8")
    participant_video.ensure_sdp_file(2, tmp_path)
    assert path.read_text(encoding="utf-8") == participant_video.sdp_text(2)


def test_source_settings_read_without_activation(tmp_path):
    settings = participant_video.source_settings(tmp_path / "slot1.sdp")
    assert settings["input"] == str(tmp_path / "slot1.sdp")
    assert settings["is_local_file"] is False
    assert settings["restart_on_activate"] is False and settings["close_when_inactive"] is False
    assert "protocol_whitelist=file,udp,rtp" in str(settings["ffmpeg_options"])
    # fflags=nobuffer would drop the first keyframe read while probing.
    assert "nobuffer" not in str(settings["ffmpeg_options"])


def test_remote_participant_source_round_trips():
    source = SourceDefinition(id="p2", kind=SourceKind.REMOTE_PARTICIPANT, name="Remote participant 2",
                              configuration=RemoteParticipantConfig(slot=2))
    record = source.to_record()
    assert record["type"] == "remote_participant"
    assert record["configuration"] == {"slot": 2}
    assert SourceDefinition.from_record(record) == source


@pytest.mark.parametrize("slot", [0, 3, True, "1", 1.0])
def test_remote_participant_rejects_bad_slots(slot):
    with pytest.raises(SceneValidationError):
        RemoteParticipantConfig.from_record({"slot": slot})
    with pytest.raises(SceneValidationError):
        RemoteParticipantConfig(slot=slot)  # type: ignore[arg-type]


def test_remote_participant_config_rejects_unknown_keys():
    with pytest.raises(SceneValidationError):
        RemoteParticipantConfig.from_record({"slot": 1, "user": "Ana"})
