from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from solin.core.integrations.automation.zoom import controls as controls_module
from solin.core.integrations.automation.zoom.controls import (
    BUTTON,
    CHECKBOX,
    LIST,
    MENUITEM,
    SPLITBUTTON,
    TABITEM,
    Automation,
    Element,
    ZoomControls,
)
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
from solin.core.integrations.automation.zoom.worker import _set_playback_microphone


def test_automation_rejects_unsupported_platform_before_importing_com(monkeypatch):
    import sys

    monkeypatch.setattr(controls_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "comtypes", None)
    with pytest.raises(ZoomAutomationError, match="unsupported_platform"):
        Automation()


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("compiled", [False, True])
def test_automation_loads_bindings_without_regenerating_packaged_modules(
    monkeypatch, generated, compiled,
):
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
    monkeypatch.setattr(controls_module, "IS_COMPILED", compiled)
    monkeypatch.setattr(controls_module, "import_module", load)
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", comtypes.client)

    if compiled and not generated:
        with pytest.raises(ImportError):
            Automation()
        comtypes.client.GetModule.assert_not_called()
        comtypes.CoCreateInstance.assert_not_called()
        return

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


@pytest.mark.parametrize(("already_muted", "same_meeting", "restored"), [
    (False, True, True),
    (True, True, False),
    (False, False, False),
])
def test_playback_only_restores_its_own_mute_in_the_same_meeting(
    already_muted, same_meeting, restored,
):
    actions = []
    microphone_enabled = not already_muted

    def set_microphone(enabled, *, disconnected_ok):
        nonlocal microphone_enabled
        assert disconnected_ok
        actions.append(enabled)
        changed = enabled != microphone_enabled
        microphone_enabled = enabled
        return changed

    original = Window(1, 2, "meeting", 100)
    current = replace(original, area=200, handle=1 if same_meeting else 3)
    controls = SimpleNamespace(
        window=original, begin_step=lambda: None, set_microphone=set_microphone,
    )
    owner = _set_playback_microphone(controls, muted=True, owner=None)
    owner = _set_playback_microphone(controls, muted=True, owner=owner)
    controls.window = current
    owner = _set_playback_microphone(controls, muted=False, owner=owner)
    assert actions == ([False, False, True] if restored else [False, False])
    assert owner is None


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
        observed = read()
        if not observed:
            raise ZoomAutomationError(kwargs["code"])
        return observed

    controls._press, controls._wait = press, wait
    if video:
        controls.set_video(True)
    else:
        assert controls.set_microphone(True)
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
    automation = Automation.__new__(Automation)
    automation.errors = (OSError,)
    automation.cache = object()
    automation.query_count = 0
    automation._element = lambda raw: raw
    automation.pattern = lambda *_: pattern
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


def _control(name, kind=BUTTON, *, left=0):
    return Element(None, name, kind, "", (left, 0, left + 10, 10))


@pytest.mark.parametrize("participants_already_open", [False, True])
def test_unrelated_lists_do_not_prevent_opening_participants(participants_already_open):
    controls, _, _ = _action_controls()
    toolbar_button = _control("Participantes")
    mute = _control("Desativar Som de Todos")
    listing = _control(
        "Lista de participantes, use a tecla de seta para navegar"
        if participants_already_open
        else "Lista de chats",
        LIST,
    )
    pressed = []
    controls.desktop.focus = lambda _: None
    controls.desktop.windows = lambda **_: []
    controls.automation.root = lambda _: object()

    def elements(_, *kinds):
        visible = [listing, mute] if pressed else [listing]
        return [element for element in visible if element.kind in kinds]

    controls.automation.elements = elements
    controls._buttons = lambda: [toolbar_button]
    controls._press = pressed.append

    if participants_already_open:
        with pytest.raises(ZoomAutomationError, match="host_controls_not_found"):
            controls._participants()
        assert not pressed
    else:
        assert mute in controls._participants()
        assert pressed == [toolbar_button]


@pytest.mark.parametrize("opens_panel_from_grid", [False, True])
def test_participants_can_open_from_the_toolbar_overflow(opens_panel_from_grid):
    controls, _, _ = _action_controls()
    toolbar = object()
    grid = object()
    popup = Window(3, controls.window.pid, "ZGridMultiLevelPopupWndClass", 100)
    more = _control("Mais controles da reunião", MENUITEM)
    toolbar_button = _control("Participantes, abrir painel, 1 participantes")
    item = _control("Participantes linha 2, coluna 3 6 de 13 fixado", TABITEM)
    unrelated = _control("Participantes adicionais linha 1, coluna 1 1 de 13 fixado", TABITEM)
    mute = _control("Desativar Som de Todos")
    opened = []
    pressed = []
    controls.desktop.focus = lambda _: None
    controls.desktop.windows = lambda *_, **__: []
    controls._toolbar = lambda: toolbar
    controls._buttons = lambda: [toolbar_button] if item in pressed else []
    controls._participant_controls = lambda: (
        [mute] if (toolbar_button in pressed or (opens_panel_from_grid and item in pressed)) else []
    )
    controls.automation.elements = lambda root, *_: [unrelated, item] if root is grid else [more]

    def open_menu(button, window=None, *, popup_class):
        opened.append((button, popup_class))
        return popup, grid

    def press(button, window=None):
        if button == item:
            assert window == popup
        pressed.append(button)
        return False

    controls._open_menu = open_menu
    controls._press = press

    assert mute in controls._participants()
    assert opened == [(more, popup.class_name)]
    assert pressed == ([item] if opens_panel_from_grid else [item, toolbar_button])


@pytest.mark.parametrize(
    ("retry_supported", "panel_opens", "count_changes"),
    [(True, True, False), (True, True, True), (True, False, False), (False, False, False)],
)
def test_participants_retries_invoke_without_clicking(retry_supported, panel_opens, count_changes):
    name = "Participants, open panel, 1 participants"
    mute = _control("Mute all")
    invocations = []
    pattern = SimpleNamespace(Invoke=lambda: invocations.append(True))
    controls, toolbar_button, clicks = _action_controls(old_name=name, new_name=name)
    cached_button = toolbar_button.raw.BuildUpdatedCache(controls.automation.cache)

    def current_name():
        return "Participants, open panel, 2 participants" if count_changes and invocations else name

    toolbar_button.raw.BuildUpdatedCache = lambda _: replace(cached_button, name=current_name())
    controls.automation.pattern = lambda *_: pattern if not invocations or retry_supported else None
    controls.desktop.focus = lambda _: None
    controls.desktop.windows = lambda *_, **__: []
    controls._buttons = lambda: [replace(toolbar_button, name=current_name())]
    controls._participant_controls = lambda: [mute] if panel_opens and len(invocations) == 2 else []

    def wait(read, *, code, **_):
        value = read()
        if not value:
            raise ZoomAutomationError(code)
        return value

    controls._wait = wait

    if panel_opens:
        assert mute in controls._participants()
    else:
        with pytest.raises(ZoomAutomationError, match="participants_panel_not_opened"):
            controls._participants()
    assert len(invocations) == (2 if retry_supported else 1)
    assert not clicks


def test_mute_all_does_not_confirm_an_unverified_self_unmute_permission():
    controls, _, _ = _action_controls()
    controls.desktop.focus = lambda _: None
    mute = _control("Mute all")
    controls._participant_controls = lambda: [mute]
    controls._popup = lambda *_: (controls.window, object())
    pressed = []
    controls._press = lambda *args: pressed.append(args)
    toggled = []
    controls.automation.elements = lambda _, kind: (
        [_control("Allow participants to unmute themselves", CHECKBOX)]
        if kind == CHECKBOX
        else [_control("Yes")]
    )
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


@pytest.mark.parametrize("join", [True, False])
def test_unknown_audio_never_selects_a_control_by_position_or_shortcut(join):
    controls, _, _ = _action_controls()
    controls._buttons = lambda: [
        _control("Other action" if join else "Other action, Alt+Q"),
        _control("Start my video"),
    ]
    controls.desktop.focus = lambda _: None
    controls._press = lambda *_: pytest.fail("Pressed an unrecognized audio control")
    controls._toolbar = lambda: None
    controls.automation.elements = lambda *_: [_control("Other menu", MENUITEM)]
    controls._open_menu = lambda *_: pytest.fail("Opened a menu for unknown audio")

    with pytest.raises(ZoomAutomationError, match="audio_control_not_found"):
        (controls.join_audio if join else controls.leave_audio)()


@pytest.mark.parametrize("matching_items", [0, 1, 2])
def test_leave_audio_uses_only_unambiguous_localized_menu_actions(matching_items):
    controls, _, _ = _action_controls()
    buttons = [_control("Desativar som, som ativado no momento")]
    controls._buttons = lambda: buttons
    toolbar = object()
    menu = object()
    controls._toolbar = lambda: toolbar
    audio_menu = _control("Configurações de Áudio", MENUITEM)
    leave = _control("Sair com o áudio do computador", MENUITEM)
    controls.automation.elements = lambda root, *_: (
        [_control("Other menu", MENUITEM), audio_menu]
        if root is toolbar
        else [leave] * matching_items + [_control("Other action", MENUITEM)] * 2
    )
    opened = []
    pressed = []

    def open_menu(element):
        opened.append(element)
        return controls.window, menu

    def press(element, _):
        pressed.append(element)
        buttons[:] = [_control("Conectar áudio")]

    controls._open_menu = open_menu
    controls._press = press
    if matching_items == 1:
        controls.leave_audio()
        assert pressed == [leave]
    else:
        with pytest.raises(ZoomAutomationError, match="leave_audio_control_not_found"):
            controls.leave_audio()
        assert not pressed
    assert opened == [audio_menu]


def test_mute_all_recognizes_localized_controls_with_shortcut_hidden():
    controls, _, _ = _action_controls()
    mute = _control("Desativar Som de Todos")
    wrong = _control("Mute all upon entry, Alt+M")
    controls._participant_controls = lambda: [wrong, mute]
    controls.desktop.focus = lambda _: None
    pressed = []
    controls._press = lambda element, _: pressed.append(element)
    dialog = object()
    controls._popup = lambda *_: (controls.window, dialog)
    permission = _control("Permitir que participantes ativem seu próprio som", CHECKBOX)
    confirm = _control("Sim", left=20)
    cancel = _control("Não", left=0)
    controls.automation.elements = lambda _, kind: (
        [_control("Other option", CHECKBOX), permission] if kind == CHECKBOX else [cancel, confirm]
    )
    controls.automation.pattern = lambda element, _: (
        SimpleNamespace(CurrentToggleState=0) if element == permission else None
    )
    controls.desktop.windows = lambda *_, **__: []

    controls.mute_all(False)

    assert pressed == [mute, confirm]


def test_ask_all_to_unmute_matches_the_localized_more_button_and_menu_action():
    controls, _, _ = _action_controls()
    mute = _control("Mute all, Alt+M")
    more = _control("Mais opções para gerenciar todos os participantes", SPLITBUTTON)
    wrong_more = _control("Mais opções de intérprete", SPLITBUTTON)
    controls._participant_controls = lambda: [mute, wrong_more, more]
    opened = []

    def open_menu(element, window):
        opened.append(element)
        return window, object()

    controls._open_menu = open_menu
    ask = _control("Pedir a todos para ativar o som", MENUITEM)
    controls.automation.elements = lambda *_: [_control("Other action", MENUITEM), ask]
    controls.automation.pattern = lambda *_: None
    controls.desktop.windows = lambda *_, **__: []
    pressed = []
    controls._press = lambda element, _: pressed.append(element)

    controls.ask_all_to_unmute()

    assert opened == [more]
    assert pressed == [ask]
