from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from solin.core.integrations.automation.zoom import controls as controls_module
from solin.core.integrations.automation.zoom.controls import Automation, Element, ZoomControls
from solin.core.integrations.automation.zoom.native import Window
from solin.core.integrations.automation.zoom.sequences import run_sequence
from solin.core.integrations.automation.zoom.state import (
    AudioState,
    MeetingState,
    SequenceResult,
    StepFailure,
    VideoState,
    ZoomAutomationError,
)
from solin.core.integrations.automation.zoom.text_match import (
    _audio_button_state_from_text,
    _video_button_state_from_text,
)


def test_automation_rejects_unsupported_platform_before_importing_com(monkeypatch):
    import sys

    monkeypatch.setattr(controls_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "comtypes", None)
    with pytest.raises(ZoomAutomationError, match="unsupported_platform"):
        Automation()


@pytest.mark.parametrize("generated", [False, True])
def test_automation_uses_packaged_bindings_or_generates_them_on_first_run(monkeypatch, generated):
    import sys
    from types import ModuleType
    from unittest.mock import Mock

    api = ModuleType("comtypes.gen.UIAutomationClient")
    api.CUIAutomation8 = SimpleNamespace(_reg_clsid_="automation-clsid")
    api.IUIAutomation2 = object()
    cache = SimpleNamespace(AddProperty=Mock())
    client = SimpleNamespace(CreateCacheRequest=lambda: cache)
    comtypes = ModuleType("comtypes")
    comtypes.__path__ = []
    comtypes.COMError = type("COMError", (OSError,), {})
    comtypes.CLSCTX_INPROC_SERVER = 1
    comtypes.CoCreateInstance = Mock(return_value=client)
    comtypes.client = ModuleType("comtypes.client")
    comtypes.client.GetModule = Mock(return_value=api)
    load = Mock(return_value=api, side_effect=None if generated else ImportError)
    monkeypatch.setattr(controls_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(controls_module, "import_module", load)
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", comtypes.client)

    automation = Automation()

    assert automation.api is api
    assert automation.client is client
    assert automation.cache is cache
    comtypes.CoCreateInstance.assert_called_once_with(
        api.CUIAutomation8._reg_clsid_, interface=api.IUIAutomation2,
        clsctx=comtypes.CLSCTX_INPROC_SERVER,
    )
    assert client.ConnectionTimeout == client.TransactionTimeout == 2000
    assert cache.AddProperty.call_count > 0
    if generated:
        comtypes.client.GetModule.assert_not_called()
    else:
        comtypes.client.GetModule.assert_called_once_with("UIAutomationCore.dll")


@pytest.mark.parametrize(
    ("label", "state"),
    [
        ("Mute, currently unmuted, Alt+A", AudioState.UNMUTED),
        ("Unmute, currently muted, Alt+A", AudioState.MUTED),
        ("Conectar áudio", AudioState.DISCONNECTED),
        ("Unknown action", AudioState.UNKNOWN),
        ("", AudioState.UNKNOWN),
    ],
)
def test_audio_recognition_does_not_guess_disconnected(label, state):
    assert _audio_button_state_from_text(label) is state


def test_video_recognition_and_immutable_state():
    assert _video_button_state_from_text("Start my video, Alt+V") is VideoState.STOPPED
    assert _video_button_state_from_text("Stop my video, Alt+V") is VideoState.STARTED
    with pytest.raises(FrozenInstanceError):
        MeetingState().in_meeting = True
    assert not SequenceResult("pre_meeting", cancelled=True).ok
    assert not SequenceResult("pre_meeting", (StepFailure("video", "unavailable"),)).ok


class Controls:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def begin_step(self):
        pass

    def check_meeting(self):
        if self.failures.get("meeting"):
            raise ZoomAutomationError("meeting_not_found")

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.failures:
            raise ZoomAutomationError(self.failures[name])

    def join_audio(self):
        self._call("join")

    def leave_audio(self):
        self._call("leave")

    def set_microphone(self, enabled):
        self._call("microphone", enabled)

    def set_video(self, enabled):
        self._call("video", enabled)

    def mute_all(self, allow_unmute):
        self._call("mute_all", allow_unmute)

    def ask_all_to_unmute(self):
        self._call("ask_all")


@pytest.mark.parametrize(
    ("sequence", "calls"),
    [
        ("pre_meeting", [("join",), ("microphone", True), ("video", True), ("mute_all", False)]),
        ("post_meeting", [("leave",), ("video", False), ("mute_all", True), ("ask_all",)]),
    ],
)
def test_sequences_preserve_order_and_permission_semantics(sequence, calls):
    controls = Controls()
    progress = []
    result = run_sequence(controls, sequence, progress.append)
    assert result.ok
    assert controls.calls == calls
    assert len(progress) == 4


def test_failure_does_not_skip_independent_safety_steps():
    controls = Controls({"video": "camera_unavailable"})
    result = run_sequence(controls, "pre_meeting")
    assert result.failed_steps == (StepFailure("video_on", "camera_unavailable"),)
    assert controls.calls[-1] == ("mute_all", False)


def test_meeting_loss_stops_the_sequence():
    controls = Controls({"meeting": True})
    result = run_sequence(controls, "pre_meeting")
    assert result.failed_steps[0].code == "meeting_not_found"
    assert controls.calls == []


def test_invalid_sequence_does_not_touch_controls():
    controls = Controls()
    with pytest.raises(ValueError):
        run_sequence(controls, "start_share")
    assert controls.calls == []


def test_queries_fetch_only_cached_properties_in_one_filtered_request():
    automation = Automation.__new__(Automation)
    automation.query_count = 0
    automation.cache = object()
    conditions = []
    automation.client = SimpleNamespace(
        CreateTrueCondition=lambda: "all",
        CreatePropertyCondition=lambda prop, kind: (prop, kind),
        CreateOrCondition=lambda first, second: (first, second),
    )
    raw = SimpleNamespace(
        CachedName="Mute",
        CachedControlType=50000,
        CachedClassName="",
        CachedBoundingRectangle=SimpleNamespace(left=1, top=2, right=9, bottom=10),
    )
    found = SimpleNamespace(Length=1, GetElement=lambda index: raw)

    def find(scope, condition, cache):
        conditions.append((scope, condition, cache))
        return found

    root = Element(SimpleNamespace(FindAllBuildCache=find), "", 0, "", (0, 0, 10, 10))
    elements = automation.elements(root, 50000, 50011)
    assert len(conditions) == automation.query_count == 1
    assert conditions[0][1] == ((30003, 50000), (30003, 50011))
    assert elements[0].name == "Mute"


def test_inspect_does_not_reveal_or_move_focus():
    window = Window(1, 2, "meeting", 20)
    desktop = SimpleNamespace(meeting=lambda: window, valid=lambda _: True)
    automation = SimpleNamespace(root=lambda _: object(), find_class=lambda *_: None)
    controls = ZoomControls(desktop, automation)
    assert controls.inspect() == MeetingState(in_meeting=True)


@pytest.mark.parametrize(
    ("operation", "name"),
    [
        (lambda controls: controls.join_audio(), "Mute, Alt+A"),
        (lambda controls: controls.leave_audio(), "Join audio"),
        (lambda controls: controls.set_microphone(True), "Mute, Alt+A"),
        (lambda controls: controls.set_video(True), "Stop my video, Alt+V"),
    ],
)
def test_already_matching_state_never_invokes_or_focuses(operation, name):
    window = Window(1, 2, "meeting", 20)
    desktop = SimpleNamespace(meeting=lambda: window, valid=lambda _: True)
    controls = ZoomControls(desktop, SimpleNamespace())
    element = Element(None, name, 50000, "", (0, 0, 10, 10))
    controls._audio = lambda: element
    controls._video = lambda: element
    operation(controls)


@pytest.mark.parametrize("video", [True, False])
def test_media_state_reconciles_a_successful_dispatch_ignored_by_busy_provider(video):
    controls, element, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    state = [VideoState.STOPPED if video else AudioState.MUTED]
    wanted = VideoState.STARTED if video else AudioState.UNMUTED
    presses = []
    def find():
        label = (
            "Stop my video" if state[0] == VideoState.STARTED else "Start my video"
        ) if video else (
            "Mute" if state[0] == AudioState.UNMUTED else "Unmute"
        )
        return Element(None, label, 50000, "", element.rect)

    controls._video = controls._audio = find

    def press(_):
        presses.append(True)
        if len(presses) == 2:
            state[0] = wanted
        return True

    def wait(read, **kwargs):
        if not read():
            raise ZoomAutomationError(kwargs["code"])

    controls._press, controls._wait = press, wait
    if video:
        controls.set_video(True)
    else:
        controls.set_microphone(True)
    assert state[0] == wanted
    assert len(presses) == 2


def test_media_reconciliation_observes_late_completion_before_dispatching_again():
    controls, element, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    reads = []
    presses = []

    def find():
        reads.append(True)
        label = "Stop my video" if len(reads) >= 3 else "Start my video"
        return Element(None, label, 50000, "", element.rect)

    def wait(read, **kwargs):
        raise ZoomAutomationError(kwargs["code"])

    controls._press = lambda _: presses.append(True)
    controls._wait = wait
    controls._video = find
    controls.set_video(True)
    assert len(presses) == 1


def test_media_reconciliation_is_bounded_when_camera_never_changes():
    controls, element, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    presses = []
    controls._press = lambda _: presses.append(True)

    def wait(read, **kwargs):
        raise ZoomAutomationError(kwargs["code"])

    controls._wait = wait
    controls._video = lambda: Element(None, "Start my video", 50000, "", element.rect)
    with pytest.raises(ZoomAutomationError, match="video_not_changed"):
        controls.set_video(True)
    assert len(presses) == 3


def test_media_reconciliation_never_replays_an_uncertain_com_dispatch():
    controls, element, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    controls._video = lambda: Element(None, "Start my video", 50000, "", element.rect)
    presses = []

    def press(_):
        presses.append(True)
        raise ZoomAutomationError("invoke_failed")

    controls._press = press
    with pytest.raises(ZoomAutomationError, match="invoke_failed"):
        controls.set_video(True)
    assert len(presses) == 1


@pytest.mark.parametrize("label", ["Join audio", "Unknown action"])
def test_microphone_rechecks_recognized_state_after_focus(label):
    controls, element, clicks = _action_controls()
    controls.desktop.focus = lambda _: None
    labels = iter(["Unmute", label])
    controls._audio = lambda: Element(None, next(labels), 50000, "", element.rect)
    with pytest.raises(ZoomAutomationError, match="audio_state_unknown"):
        controls.set_microphone(True)
    assert not clicks


def _action_controls(*, old_name="Unmute", new_name="Unmute", pattern=None):
    window = Window(1, 2, "meeting", 20)
    clicks = []
    desktop = SimpleNamespace(
        meeting=lambda: window, valid=lambda _: True, click=lambda *args: clicks.append(args)
    )
    updated = Element(SimpleNamespace(CachedIsEnabled=True), new_name, 50000, "", (20, 30, 40, 50))
    updated.raw.BuildUpdatedCache = lambda _: updated
    original = Element(
        SimpleNamespace(BuildUpdatedCache=lambda _: updated), old_name, 50000, "", (0, 0, 10, 10)
    )
    automation = SimpleNamespace(
        errors=(OSError,),
        cache=object(),
        query_count=0,
        _element=lambda raw: raw,
        pattern=lambda *_: pattern,
    )
    return ZoomControls(desktop, automation), original, clicks


def test_changed_action_is_rejected_before_invoke_or_input():
    controls, element, clicks = _action_controls(new_name="Mute")
    with pytest.raises(ZoomAutomationError, match="control_changed"):
        controls._press(element)
    assert clicks == []


def test_failed_invoke_never_repeats_a_toggle_with_native_input():
    invoked = []

    def uncertain_invoke():
        invoked.append(True)
        raise OSError("provider disconnected after dispatch")

    controls, element, clicks = _action_controls(pattern=SimpleNamespace(Invoke=uncertain_invoke))
    with pytest.raises(ZoomAutomationError, match="invoke_failed"):
        controls._press(element)
    assert invoked == [True]
    assert clicks == []


def test_explicitly_unsupported_invoke_can_use_verified_native_input():
    def unsupported():
        error = OSError("UIA_E_NOTSUPPORTED")
        error.hresult = -2147220992
        raise error

    controls, element, clicks = _action_controls(pattern=SimpleNamespace(Invoke=unsupported))
    assert not controls._press(element)
    assert len(clicks) == 1


def test_native_fallback_uses_fresh_control_geometry():
    controls, element, clicks = _action_controls()
    assert not controls._press(element)
    assert clicks == [(controls.window, (20, 30, 40, 50))]


def test_floating_meeting_toolbar_can_be_read_without_a_desktop_tree_walk():
    from solin.core.integrations.automation.zoom.native import FLOATING_CLASS

    window = Window(1, 2, FLOATING_CLASS, 20)
    desktop = SimpleNamespace(meeting=lambda: window, valid=lambda _: True)
    root = object()
    elements = [
        Element(None, "Unmute, Alt+A", 50000, "", (0, 0, 10, 10)),
        Element(None, "Start my video, Alt+V", 50000, "", (0, 0, 10, 10)),
    ]
    automation = SimpleNamespace(root=lambda _: root, elements=lambda *_: elements)
    controls = ZoomControls(desktop, automation)
    assert controls.inspect() == MeetingState(AudioState.MUTED, VideoState.STOPPED, True)


@pytest.mark.parametrize("invoked", [True, False])
def test_menu_fallback_does_not_repeat_an_existing_native_click(invoked):
    controls, element, clicks = _action_controls()
    controls.desktop.focus = lambda _: None
    controls._press = lambda *_: invoked
    calls = []

    def popup(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ZoomAutomationError("dialog_not_found")
        return "menu"

    controls._popup = popup
    assert controls._open_menu(element) == "menu"
    assert len(clicks) == int(invoked)


def test_mute_all_does_not_confirm_an_unverified_self_unmute_permission():
    controls, element, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    controls._participants = lambda: [element]
    controls._mute_button = lambda _: element
    controls._popup = lambda *_: (controls.window, element)
    pressed = []
    controls._press = lambda *args: pressed.append(args)
    toggled = []
    controls.automation.elements = lambda *_: [element]
    controls.automation.pattern = lambda *_: SimpleNamespace(
        CurrentToggleState=1, Toggle=lambda: toggled.append(True)
    )

    def verified(read, **kwargs):
        if not read():
            raise ZoomAutomationError(kwargs["code"])

    controls._wait = verified
    with pytest.raises(ZoomAutomationError, match="allow_unmute_not_changed"):
        controls.mute_all(False)
    assert toggled == [True]
    assert len(pressed) == 1  # Only opens the dialog; no confirmation is sent.
