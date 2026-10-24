"""Remote part-holders' video slots, shared by the bridge and the libobs sidecar.

``solin-galene-bridge receive --video-ports`` forwards each part-holder's H.264 as
plain RTP (payload type 96, one continuous stream per slot) to a fixed loopback
port. A scene shows a slot with a "Remote participant" source: an ``ffmpeg_source``
reading the slot's SDP file. The ports are fixed so a scene built before the meeting
starts is already listening when the first part-holder turns on their camera.
"""

from __future__ import annotations

import getpass
import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

SLOT_COUNT = 2
# Even ports (RTP) with the odd one above free for RTCP, outside the usual
# ephemeral ranges and clear of Galene's and Solin's own listeners.
SLOT_PORTS: tuple[int, ...] = (47110, 47112)

# Low-latency reading of a local RTP stream. ``buffer_size`` lets a keyframe burst
# through the kernel socket buffer intact; ``reorder_queue_size=0`` because loopback
# never reorders.
FFMPEG_OPTIONS = (
    "protocol_whitelist=file,udp,rtp flags=low_delay probesize=32768 analyzeduration=0 "
    "reorder_queue_size=0 buffer_size=8388608"
)


def valid_slot(slot: object) -> bool:
    return isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= SLOT_COUNT


def slot_port(slot: int) -> int:
    if not valid_slot(slot):
        raise ValueError(f"no video slot {slot!r}")
    return SLOT_PORTS[slot - 1]


def bridge_ports_argument() -> str:
    return ",".join(str(port) for port in SLOT_PORTS)


def sdp_text(slot: int) -> str:
    port = slot_port(slot)
    return "\n".join(
        (
            "v=0",
            "o=- 0 0 IN IP4 127.0.0.1",
            f"s=Solin remote participant {slot}",
            "c=IN IP4 127.0.0.1",
            "t=0 0",
            f"m=video {port} RTP/AVP 96",
            "a=rtpmap:96 H264/90000",
            "a=fmtp:96 packetization-mode=1",
            "a=recvonly",
            "",
        )
    )


def default_sdp_directory() -> Path:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name in odd environments
        user = "user"
    return Path(tempfile.gettempdir()) / f"solin-meeting-video-{user}"


def ensure_sdp_file(slot: int, directory: Path | None = None) -> Path:
    """Write (if missing or stale) and return the slot's SDP file."""

    folder = directory or default_sdp_directory()
    path = folder / f"slot{slot}.sdp"
    text = sdp_text(slot)
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def source_settings(sdp_path: Path) -> dict[str, object]:
    """``ffmpeg_source`` settings for one slot."""

    return {
        "is_local_file": False,
        "input": str(sdp_path),
        "ffmpeg_options": FFMPEG_OPTIONS,
        "reconnect_delay_sec": 1,
        "buffering_mb": 0,
        "hw_decode": False,
        # Read from creation, not only while on an output channel: the editor
        # preview and the projection route only show the source (see rtsp_camera).
        "restart_on_activate": False,
        "close_when_inactive": False,
        "clear_on_media_end": True,
    }
