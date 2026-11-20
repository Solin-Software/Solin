"""Passive participant snapshots and explicit, row-scoped Zoom actions."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_hex

from . import i18n_labels as labels
from .controls import BUTTON, TEXT, Element, ZoomControls
from .native import Window
from .state import (
    AudioState,
    ParticipantAction,
    ParticipantActionResult,
    ParticipantStepResult,
    ParticipantStepStatus,
    RaisedHand,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
    StepFailure,
    ZoomAutomationError,
)
from .text_match import _normalize_label


def _texts(variants: list[str]) -> frozenset[str]:
    return frozenset(map(_normalize_label, variants))


def _number_patterns(variants: list[str]) -> str:
    return "|".join(re.escape(text).replace("%d", r"(\d+)") for text in sorted(_texts(variants)))


def _number(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    value = next(group for group in match.groups() if group is not None)
    return int(value) if len(value) <= 6 else None


_SEPARATOR = re.compile(r"\s*[,，、]\s*")
_COUNT = re.compile(_number_patterns(labels.PARTICIPANT_COUNT_TEXT))
_QUEUE_FIELD = re.compile(
    rf"(?:^|{_SEPARATOR.pattern})(?:{_number_patterns(labels.PARTICIPANT_HAND_QUEUE_TEXT)})"
    rf"(?={_SEPARATOR.pattern}|$)"
)
_HAND = _texts(labels.PARTICIPANT_HAND_RAISED_TEXT)
_HINT_END = re.compile(
    rf"{_SEPARATOR.pattern}(?:"
    + "|".join(map(re.escape, sorted(_texts(labels.PARTICIPANT_ROW_HINT_TEXT))))
    + ")$"
)
_SELF = _texts(labels.PARTICIPANT_SELF_TEXT)
_PRIORITY_ROLE = _texts(labels.PARTICIPANT_PRIORITY_ROLE_TEXT)
_ROW_STATE = _texts(labels.PARTICIPANT_ROW_STATE_TEXT)
_AUDIO = {
    text: state
    for state, variants in (
        (AudioState.MUTED, labels.PARTICIPANT_AUDIO_MUTED_TEXT),
        (AudioState.UNMUTED, labels.PARTICIPANT_AUDIO_UNMUTED_TEXT),
        (AudioState.DISCONNECTED, labels.PARTICIPANT_AUDIO_DISCONNECTED_TEXT),
    )
    for text in _texts(variants)
}
_KNOWN_FIELDS = _HAND | _SELF | _PRIORITY_ROLE | _ROW_STATE | _AUDIO.keys()
_ACTIONS = {
    "lower_hand": _texts(labels.PARTICIPANT_LOWER_HAND_TEXT),
    "mute": _texts(labels.PARTICIPANT_MUTE_TEXT),
    "request_unmute": _texts(labels.PARTICIPANT_ASK_UNMUTE_TEXT),
}


@dataclass(frozen=True, slots=True)
class _Row:
    element: Element
    name: str
    audio: AudioState
    hand_raised: bool | None
    is_self: bool
    priority_role: bool
    queue_position: int | None


def _read_row(element: Element, names: tuple[str, ...]) -> _Row | None:
    # The display name is a separate Text node. Anchor the description to it
    # before reading any fields, since names themselves can contain commas,
    # role names, and the exact localized text for a raised hand.
    tails = [
        (name, element.name[match.end() :])
        for name in dict.fromkeys(names)
        if element.name.startswith(name) and (match := _SEPARATOR.match(element.name, len(name)))
    ]
    if len(tails) != 1:
        return None
    name, description = tails[0]
    description = _normalize_label(description)
    hint = _HINT_END.search(description)
    if hint is None:
        return None
    metadata = description[: hint.start()]
    # Some localized templates contain the same commas used between fields.
    # Match those templates in full before splitting the remaining fields.
    positions = tuple(_number(match) for match in _QUEUE_FIELD.finditer(metadata))
    position = positions[0] if len(positions) == 1 and positions[0] else None
    details = {
        field.strip().removeprefix("(").removesuffix(")")
        for field in _SEPARATOR.split(_QUEUE_FIELD.sub("", metadata))
        if field.strip()
    }
    audio_states = {_AUDIO[field] for field in details if field in _AUDIO}
    audio = next(iter(audio_states)) if len(audio_states) == 1 else AudioState.UNKNOWN
    raised = None
    if details & _HAND:
        raised = True
    elif not (details - _KNOWN_FIELDS or positions) and audio is not AudioState.UNKNOWN:
        raised = False
    return _Row(
        element,
        name,
        audio,
        raised,
        bool(details & _SELF),
        bool(details & _PRIORITY_ROLE),
        position,
    )


@dataclass(frozen=True, slots=True)
class _Panel:
    window: Window
    listing: Element
    heading: Element | None


@dataclass(frozen=True, slots=True)
class _Roster:
    panel: _Panel
    rows: tuple[_Row, ...]
    complete: bool


def _read_heading(
    controls: ZoomControls, window: Window, cached: Element | None
) -> tuple[Element | None, int | None]:
    automation = controls.automation
    if cached is not None:
        try:
            heading = automation.refresh(cached)
        except automation.errors:
            # The heading can be replaced independently when the panel redraws.
            heading = None
        if heading is not None:
            count = _number(_COUNT.fullmatch(_normalize_label(heading.name)))
            if count is not None and not heading.raw.CachedIsOffscreen:
                return heading, count

    root = automation.root(window.handle)
    if window.handle != controls.window.handle:
        count = _number(_COUNT.fullmatch(_normalize_label(root.name)))
        if count is not None:
            return root, count
    headings = [
        (element, count)
        for element in automation.elements(root, TEXT)
        if (count := _number(_COUNT.fullmatch(_normalize_label(element.name)))) is not None
        and automation.outside_list(element, root)
    ]
    return headings[0] if len(headings) == 1 else (None, None)


def _ordered_hands(roster: _Roster) -> tuple[list[_Row], bool]:
    hands = [row for row in roster.rows if row.hand_raised]
    positions = {row.queue_position: row for row in hands}
    if positions and None not in positions and len(positions) == len(hands):
        return sorted(hands, key=lambda row: row.queue_position or 0), roster.complete
    # The documented queue order applies to the regular raised-hand group.
    # Zoom lists self, host and co-hosts in their own groups ahead of it.
    is_ordered = len(hands) <= 1 or not any(row.is_self or row.priority_role for row in hands)
    return hands, roster.complete and is_ordered


class ZoomRaisedHands:
    """Keep a bounded set of current targets in the worker's COM apartment."""

    def __init__(self) -> None:
        self._meeting: Window | None = None
        self._panel: _Panel | None = None
        self._targets: dict[str, _Row] = {}

    @staticmethod
    def _same_window(first: Window | None, second: Window) -> bool:
        return first is not None and (first.handle, first.pid, first.class_name) == (
            second.handle,
            second.pid,
            second.class_name,
        )

    def _same_panel(self, controls: ZoomControls, window: Window, listing: Element) -> bool:
        return (
            self._same_window(self._meeting, controls.window)
            and self._panel is not None
            and self._same_window(self._panel.window, window)
            and controls.automation.compare(self._panel.listing, listing)
        )

    def _read_roster(self, controls: ZoomControls, *, open_if_closed: bool = False) -> _Roster:
        participant_list = controls.participant_list(open_if_closed=open_if_closed)
        if participant_list is None:
            raise ZoomAutomationError("panel_closed")
        window, listing = participant_list
        cached = (
            self._panel.heading
            if self._panel and self._same_panel(controls, window, listing)
            else None
        )
        entries = controls.automation.participant_rows(listing)
        rows = tuple(
            row for element, names in entries if (row := _read_row(element, names)) is not None
        )
        heading, count = _read_heading(controls, window, cached)
        complete = count == len(rows) == len(entries) and all(
            row.hand_raised is not None for row in rows
        )
        return _Roster(_Panel(window, listing, heading), rows, complete)

    def clear(self) -> None:
        self._meeting = None
        self._panel = None
        self._targets.clear()

    def snapshot(self, controls: ZoomControls, *, open_if_closed: bool) -> RaisedHandsSnapshot:
        try:
            return self._snapshot(controls, open_if_closed=open_if_closed)
        except (ZoomAutomationError, *controls.automation.errors):
            self.clear()
            raise

    def _snapshot(self, controls: ZoomControls, *, open_if_closed: bool) -> RaisedHandsSnapshot:
        roster = self._read_roster(controls, open_if_closed=open_if_closed)
        reusable = self._same_panel(controls, roster.panel.window, roster.panel.listing)
        previous = (
            {row.name: (reference, row) for reference, row in self._targets.items()}
            if reusable
            else {}
        )
        counts = Counter(row.name for row in roster.rows)
        hands, chronological = _ordered_hands(roster)
        displayed = []
        targets: dict[str, _Row] = {}
        for row in hands:
            reference = None
            # No stable participant ID is exposed by all Zoom versions.
            # Require both a unique display name in the complete roster and
            # provider identity. Handlers must never retarget by name.
            if roster.complete and not row.is_self and counts[row.name] == 1:
                prior = previous.get(row.name)
                reference = (
                    prior[0]
                    if prior and controls.automation.compare(prior[1].element, row.element)
                    else token_hex(16)
                )
                targets[reference] = row
            displayed.append(RaisedHand(row.name, row.audio, reference))
        self._meeting = controls.window
        self._panel = roster.panel
        self._targets = targets
        status = RaisedHandsStatus.READY if roster.complete else RaisedHandsStatus.PARTIAL
        return RaisedHandsSnapshot(status, tuple(displayed), chronological)

    def _resolve(self, controls: ZoomControls, target: _Row) -> _Row:
        roster = self._read_roster(controls)
        if not self._same_panel(controls, roster.panel.window, roster.panel.listing):
            self.clear()
            raise ZoomAutomationError("participant_changed")
        if not roster.complete:
            raise ZoomAutomationError("participants_list_partial")
        matches = [row for row in roster.rows if row.name == target.name]
        if len(matches) != 1 or not controls.automation.compare(matches[0].element, target.element):
            raise ZoomAutomationError("participant_changed")
        return matches[0]

    def _invoke(self, controls: ZoomControls, target: _Row, step: str) -> ParticipantStepStatus:
        row = self._resolve(controls, target)
        if step == "lower_hand" and row.hand_raised is False:
            return ParticipantStepStatus.ALREADY_SET
        if step != "lower_hand":
            if not row.hand_raised:
                raise ZoomAutomationError("hand_no_longer_raised")
            wanted = AudioState.MUTED if step == "mute" else AudioState.UNMUTED
            if row.audio is wanted:
                return ParticipantStepStatus.ALREADY_SET
            if row.audio not in (AudioState.MUTED, AudioState.UNMUTED):
                raise ZoomAutomationError("participant_audio_unavailable")

        matches = [
            button
            for button in controls.automation.elements(row.element, BUTTON)
            if _normalize_label(button.name) in _ACTIONS[step]
        ]
        if len(matches) != 1:
            raise ZoomAutomationError("participant_control_unavailable")
        # Revalidate the target again after enumerating the controls. This
        # rejects a row that moved or was recycled during the provider call.
        current = self._resolve(controls, target)
        if current.hand_raised != row.hand_raised or current.audio != row.audio:
            raise ZoomAutomationError("participant_state_changed")
        if not controls._press(matches[0], click_fallback=False):
            raise ZoomAutomationError("participant_invoke_unsupported")
        if step == "request_unmute":
            return ParticipantStepStatus.REQUESTED
        controls._wait(
            lambda: (
                self._resolve(controls, target).hand_raised is False
                if step == "lower_hand"
                else self._resolve(controls, target).audio is AudioState.MUTED
            ),
            timeout=2,
            code="participant_state_not_changed",
        )
        return ParticipantStepStatus.CHANGED

    def act(
        self,
        controls: ZoomControls,
        action: ParticipantAction,
        reference: str,
        progress: Callable[[ParticipantStepResult], None],
    ) -> ParticipantActionResult:
        target = self._targets.get(reference)
        if target is None:
            return ParticipantActionResult(
                action, reference, failure=StepFailure(action.steps[0], "participant_changed")
            )
        completed = []
        for step in action.steps:
            controls.begin_step()
            try:
                status = self._invoke(controls, target, step)
            except ZoomAutomationError as exc:
                if exc.code in {
                    "meeting_not_found",
                    "panel_closed",
                    "participants_list_unavailable",
                }:
                    self.clear()
                return ParticipantActionResult(
                    action, reference, tuple(completed), StepFailure(step, exc.code)
                )
            except controls.automation.errors:
                self.clear()
                return ParticipantActionResult(
                    action, reference, tuple(completed), StepFailure(step, "automation_failed")
                )
            result = ParticipantStepResult(step, status)
            completed.append(result)
            progress(result)
        if action is not ParticipantAction.REQUEST_UNMUTE:
            self._targets.pop(reference, None)
        return ParticipantActionResult(action, reference, tuple(completed))
