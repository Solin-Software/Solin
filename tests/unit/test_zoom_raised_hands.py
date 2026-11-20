from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.integrations.automation.zoom.controls import (
    BUTTON,
    LIST,
    LISTITEM,
    TEXT,
    Element,
    ZoomControls,
)
from solin.core.integrations.automation.zoom.native import Window
from solin.core.integrations.automation.zoom.participants import ZoomRaisedHands
from solin.core.integrations.automation.zoom.state import (
    AudioState,
    ParticipantAction,
    ParticipantStepStatus,
    RaisedHandsStatus,
    ZoomAutomationError,
)


def _element(name, kind=BUTTON, identity=None):
    return Element(identity, name, kind, "", (0, 0, 1, 1))


def _participant(name, *, hand=True, audio="Computer audio muted", role="", identity=None):
    details = [
        role,
        audio,
        "Video off",
        "Hand raised" if hand else "",
        "Press Tab for more options",
    ]
    description = ",".join(detail for detail in details if detail)
    return _element(f"{name}, {description}", LISTITEM, identity or object()), (name,)


class _Zoom:
    def __init__(self, *entries):
        self.entries = list(entries)
        self.panel_open = True
        self.count = len(entries)
        self.window = Window(1, 2, "meeting", 100)
        self.panel = self.window
        self.listing = _element("Participant list", LIST, object())
        self.exposed_buttons = []
        self.on_invoke = Mock(return_value=True)
        self.read_panel = Mock(side_effect=self._list)
        self.automation = SimpleNamespace(
            compare=lambda first, second: first.raw is second.raw,
            elements=self._elements,
            participant_rows=lambda _: self.entries.copy(),
            outside_list=lambda element, _root: element.raw is None,
            root=lambda _: _element("Zoom"),
            refresh=lambda element: replace(
                element,
                name=f"Participants ({self.count})",
                raw=SimpleNamespace(CachedIsOffscreen=False),
            ),
            errors=(OSError,),
        )

    def _list(self, *, open_if_closed):
        if self.panel_open or open_if_closed:
            return self.panel, self.listing
        return None

    def participant_list(self, *, open_if_closed=False):
        return self.read_panel(open_if_closed=open_if_closed)

    def _elements(self, _, kind):
        if kind == TEXT:
            return [_element(f"Participants ({self.count})", TEXT)]
        return self.exposed_buttons

    def _press(self, button, *, click_fallback):
        assert not click_fallback
        return self.on_invoke(button.name)

    def begin_step(self):
        pass

    def _wait(self, confirmation, *, timeout, code):
        if not confirmation():
            raise ZoomAutomationError(code)

    def change(self, name, **state):
        for index, (element, (display_name,)) in enumerate(self.entries):
            if display_name == name:
                self.entries[index] = _participant(name, identity=element.raw, **state)
                return
        raise AssertionError("Test attempted to change an absent participant")


def test_names_with_delimiters_and_official_state_text_do_not_manufacture_hands():
    target = _participant("Literal, Hand raised, alias", hand=False)
    french = (
        _element(
            "Zoé, Ordinateur mis en sourdine,Vidéo désactivée,Main levée,"
            "Appuyez sur Tab pour plus d’options",
            LISTITEM,
            object(),
        ),
        ("Zoé",),
    )
    controls = _Zoom(target, french)

    snapshot = ZoomRaisedHands().snapshot(controls, open_if_closed=False)

    assert snapshot.status is RaisedHandsStatus.READY
    assert [hand.name for hand in snapshot.hands] == ["Zoé"]
    assert snapshot.hands[0].audio is AudioState.MUTED


def test_terminal_hint_with_comma_is_read_as_one_localized_field():
    german = (
        _element(
            "Jana, Bei Computeraudio Stummschaltung aktiviert,Video aus,Hand gehoben,"
            "Drücken Sie die Tabulatortaste, um weitere Optionen anzuzeigen",
            LISTITEM,
            object(),
        ),
        ("Jana",),
    )
    snapshot = ZoomRaisedHands().snapshot(_Zoom(german), open_if_closed=False)

    assert snapshot.status is RaisedHandsStatus.READY
    assert [hand.name for hand in snapshot.hands] == ["Jana"]
    assert snapshot.hands[0].reference is not None


@pytest.mark.parametrize("detached", [False, True])
def test_participants_list_can_be_read_without_host_buttons(detached):
    meeting = Window(1, 2, "meeting", 100)
    popup = Window(2, meeting.pid, "ZPlistPopupContainerWndClass", 20)
    host = popup if detached else meeting
    listing = _element(
        "Participant list, use arrow key to navigate",
        LIST,
        SimpleNamespace(CachedIsOffscreen=False),
    )
    desktop = SimpleNamespace(
        meeting=lambda: meeting,
        valid=lambda _: True,
        windows=lambda *_, pid: [popup] if pid == meeting.pid else [],
        focus=Mock(),
    )
    provider = SimpleNamespace(
        root=lambda handle: handle,
        elements=lambda handle, *_: [listing] if handle == host.handle else [],
    )

    assert ZoomControls(desktop, provider).participant_list() == (host, listing)
    desktop.focus.assert_not_called()


def test_complete_empty_partial_and_closed_are_distinct():
    controls = _Zoom(_participant("No hand", hand=False))
    monitor = ZoomRaisedHands()
    empty = monitor.snapshot(controls, open_if_closed=False)
    assert empty.status is RaisedHandsStatus.READY
    assert not empty.hands

    # The provider only exposes the scrolled viewport.
    controls.entries = [_participant("Visible")]
    controls.count = 8
    partial = monitor.snapshot(controls, open_if_closed=False)
    assert partial.status is RaisedHandsStatus.PARTIAL
    assert [hand.name for hand in partial.hands] == ["Visible"]
    assert partial.hands[0].reference is None
    assert not partial.chronological

    controls.panel_open = False
    with pytest.raises(ZoomAutomationError, match="panel_closed"):
        monitor.snapshot(controls, open_if_closed=False)
    controls.read_panel.assert_called_with(open_if_closed=False)


def test_participant_named_like_count_heading_does_not_prove_coverage():
    controls = _Zoom(_participant("Participants (1)"))
    native_query = controls.automation.elements
    controls.automation.elements = lambda root, kind: (
        [_element("Participants (1)", TEXT, object())] if kind == TEXT else native_query(root, kind)
    )
    snapshot = ZoomRaisedHands().snapshot(controls, open_if_closed=False)
    assert snapshot.status is RaisedHandsStatus.PARTIAL
    assert len(snapshot.hands) == 1
    assert snapshot.hands[0].reference is None


def test_unknown_row_metadata_does_not_report_a_complete_empty_list():
    participant = _participant("Unrecognized")
    element, names = participant
    unknown = replace(element, name=element.name.replace("Hand raised", "Different state"))
    snapshot = ZoomRaisedHands().snapshot(_Zoom((unknown, names)), open_if_closed=False)

    assert snapshot.status is RaisedHandsStatus.PARTIAL
    assert not snapshot.hands


def test_queue_positions_order_the_raised_hands_including_cohosts():
    cohost, cohost_names = _participant("Co-host first in panel", role="(Co-host)")
    regular, regular_names = _participant("Regular attendee")
    monitor = ZoomRaisedHands()
    controls = _Zoom((cohost, cohost_names), (regular, regular_names))

    grouped = monitor.snapshot(controls, open_if_closed=False)
    assert not grouped.chronological
    assert [hand.name for hand in grouped.hands] == ["Co-host first in panel", "Regular attendee"]

    def ranked(row, names, position):
        return replace(
            row,
            name=row.name.replace(
                "Hand raised", f"Hand raised, number {position} in the raise hands queue"
            ),
        ), names

    controls.entries = [ranked(cohost, cohost_names, 2), ranked(regular, regular_names, 1)]
    chronological = monitor.snapshot(controls, open_if_closed=False)
    assert chronological.chronological
    assert [hand.name for hand in chronological.hands] == [
        "Regular attendee",
        "Co-host first in panel",
    ]


def test_duplicate_and_local_names_are_read_only():
    controls = _Zoom(
        _participant("Same name"),
        _participant("Same name"),
        _participant("Self", role="(Host, me)"),
    )
    snapshot = ZoomRaisedHands().snapshot(controls, open_if_closed=False)

    assert snapshot.status is RaisedHandsStatus.READY
    assert [item.name for item in snapshot.hands] == ["Same name", "Same name", "Self"]
    assert all(item.reference is None for item in snapshot.hands)


def test_reference_survives_reordering_but_never_targets_a_same_name_replacement():
    controls = _Zoom(_participant("Target"), _participant("Other"))
    monitor = ZoomRaisedHands()
    reference = monitor.snapshot(controls, open_if_closed=False).hands[0].reference
    assert reference is not None
    controls.entries.reverse()
    snapshot = monitor.snapshot(controls, open_if_closed=False)
    assert snapshot.hands[1].reference == reference

    controls.entries[1] = _participant("Target")
    controls.exposed_buttons = [_element("Lower hand")]
    outcome = monitor.act(controls, ParticipantAction.LOWER_HAND, reference, Mock())

    assert outcome.failure.code == "participant_changed"
    controls.on_invoke.assert_not_called()


def test_detached_panel_change_invalidates_existing_target():
    controls = _Zoom(_participant("Target"))
    monitor = ZoomRaisedHands()
    reference = monitor.snapshot(controls, open_if_closed=False).hands[0].reference
    assert reference is not None
    controls.panel = Window(20, controls.window.pid, "detached panel", 120)
    new = monitor.snapshot(controls, open_if_closed=False).hands[0].reference

    assert new and new != reference
    outcome = monitor.act(controls, ParticipantAction.LOWER_HAND, reference, Mock())
    assert outcome.failure.code == "participant_changed"
    controls.on_invoke.assert_not_called()


@pytest.mark.parametrize("leave_after_mute", [False, True])
def test_combined_action_confirms_mute_then_resolves_the_row_again(leave_after_mute):
    controls = _Zoom(_participant("Target", audio="Computer audio unmuted"), _participant("Other"))
    monitor = ZoomRaisedHands()
    reference = monitor.snapshot(controls, open_if_closed=False).hands[0].reference
    assert reference is not None
    controls.exposed_buttons = [_element("Lower hand"), _element("Mute"), _element("Mute all")]

    def invoke(label):
        if label == "Mute":
            controls.change("Target")
            controls.entries.reverse()
        else:
            controls.change("Target", hand=False)
        return True

    controls.on_invoke.side_effect = invoke

    def progress(step):
        if leave_after_mute and step.step == "mute":
            controls.entries[1] = _participant("Target")

    result = monitor.act(controls, ParticipantAction.MUTE_AND_LOWER, reference, progress)
    executed = [call.args[0] for call in controls.on_invoke.call_args_list]
    if leave_after_mute:
        assert executed == ["Mute"]
        assert result.failure.step == "lower_hand"
        assert [step.step for step in result.completed_steps] == ["mute"]
    else:
        assert executed == ["Mute", "Lower hand"]
        assert result.ok
        assert all(step.status is ParticipantStepStatus.CHANGED for step in result.completed_steps)


@pytest.mark.parametrize("button_available", [False, True])
def test_unmute_request_requires_explicit_label_and_does_not_claim_audio_changed(button_available):
    controls = _Zoom(_participant("Target"))
    monitor = ZoomRaisedHands()
    reference = monitor.snapshot(controls, open_if_closed=False).hands[0].reference
    assert reference is not None
    controls.exposed_buttons = [_element("Unmute"), _element("Ask all to unmute")]
    if button_available:
        controls.exposed_buttons.append(_element("Ask to unmute"))

    result = monitor.act(controls, ParticipantAction.REQUEST_UNMUTE, reference, Mock())

    if button_available:
        assert result.ok
        assert result.completed_steps[0].status is ParticipantStepStatus.REQUESTED
        controls.on_invoke.assert_called_once_with("Ask to unmute")
        snapshot = monitor.snapshot(controls, open_if_closed=False)
        assert snapshot.hands[0].audio is AudioState.MUTED
    else:
        assert result.failure.code == "participant_control_unavailable"
        controls.on_invoke.assert_not_called()


def test_missing_mute_confirmation_never_repeats_or_lowers():
    controls = _Zoom(_participant("Target", audio="Computer audio unmuted"))
    monitor = ZoomRaisedHands()
    reference = monitor.snapshot(controls, open_if_closed=False).hands[0].reference
    assert reference is not None
    controls.exposed_buttons = [_element("Mute"), _element("Lower hand")]

    result = monitor.act(controls, ParticipantAction.MUTE_AND_LOWER, reference, Mock())

    assert not result.completed_steps
    assert result.failure.code == "participant_state_not_changed"
    controls.on_invoke.assert_called_once_with("Mute")
