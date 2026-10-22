"""Meeting workflows, independent of the UI Automation transport."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from solin.core.conference.provider import SEQUENCES, SequenceResult, StepFailure

from .state import ZoomAutomationError

log = logging.getLogger(__name__)


class MeetingControls(Protocol):
    def begin_step(self) -> None: ...
    def check_meeting(self) -> None: ...
    def join_audio(self) -> None: ...
    def leave_audio(self) -> None: ...
    def set_microphone(self, enabled: bool) -> None: ...
    def set_video(self, enabled: bool) -> None: ...
    def mute_all(self, allow_unmute: bool) -> None: ...
    def ask_all_to_unmute(self) -> None: ...


def run_sequence(
    controls: MeetingControls,
    sequence: str,
    progress: Callable[[str], None] = lambda _step: None,
) -> SequenceResult:
    if sequence not in SEQUENCES:
        raise ValueError("Unknown Zoom meeting sequence")
    steps = (
        [
            ("join_audio", controls.join_audio),
            ("microphone_on", lambda: controls.set_microphone(True)),
            ("video_on", lambda: controls.set_video(True)),
            ("mute_all", lambda: controls.mute_all(False)),
        ]
        if sequence == "pre_meeting"
        else [
            ("leave_audio", controls.leave_audio),
            ("video_off", lambda: controls.set_video(False)),
            ("allow_unmute", lambda: controls.mute_all(True)),
            ("ask_all_to_unmute", controls.ask_all_to_unmute),
        ]
    )
    failures = []
    for name, action in steps:
        progress(name)
        try:
            controls.begin_step()
            controls.check_meeting()
            action()
        except ZoomAutomationError as exc:
            failures.append(StepFailure(name, exc.code))
            if exc.code == "meeting_not_found":
                break
        except Exception:  # noqa: BLE001 - native provider failures become explicit outcomes
            log.exception("Zoom step failed: %s", name)
            failures.append(StepFailure(name, "automation_failed"))
    return SequenceResult(sequence, tuple(failures))
