from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ─────────────────────────────────────────────────────────────────
#  Enums e dataclass de estado
# ─────────────────────────────────────────────────────────────────

class AudioState(Enum):
    MUTED        = "muted"
    UNMUTED      = "unmuted"
    DISCONNECTED = "disconnected"
    UNKNOWN      = "unknown"


class VideoState(Enum):
    STOPPED = "stopped"
    STARTED = "started"
    UNKNOWN = "unknown"


class ShareState(Enum):
    SHARING     = "sharing"
    NOT_SHARING = "not_sharing"
    UNKNOWN     = "unknown"


@dataclass
class MeetingState:
    audio:             AudioState = AudioState.UNKNOWN
    video:             VideoState = VideoState.UNKNOWN
    sharing:           ShareState = ShareState.UNKNOWN
    audio_title:       str = ""
    video_title:       str = ""
    participant_count: int = 0
    participant_names: list[str] = field(default_factory=list)
    people_count:      int = 0
    in_meeting:        bool = False

