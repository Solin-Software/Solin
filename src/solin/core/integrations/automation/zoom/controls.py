"""Scoped native UI Automation queries and verified Zoom audio/video actions."""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from .i18n_labels import (
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    CONNECT_AUDIO_TEXT,
    PARTICIPANTS_TEXT,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
    JOIN_AUDIO_TEXT,
    LEAVE_AUDIO_TEXT,
)
from .native import FLOATING_CLASS, TOOLBAR_CLASS, Window, WindowsDesktop
from .state import AudioState, MeetingState, VideoState, ZoomAutomationError
from .text_match import (
    _audio_button_state_from_text,
    _toolbar_action_match_score,
    _video_button_state_from_text,
)

BUTTON, CHECKBOX, MENUITEM, SPLITBUTTON = 50000, 50002, 50011, 50031
INVOKE, TOGGLE = 10000, 10015
_HOTKEY = re.compile(r"(?<![\w+])[^\s,()]+(?:\+[^\s,()]+)+(?![\w+])")
_SEPARATOR = re.compile(r"^\d+ of \d+$")
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Element:
    raw: Any
    name: str
    kind: int
    class_name: str
    rect: tuple[int, int, int, int]


class Automation:
    """One COM apartment owns all interfaces, cache requests and elements."""

    api: ModuleType
    client: Any
    cache: Any
    errors: tuple[type[Exception], ...]
    query_count: int

    def __init__(self):
        if sys.platform != "win32":
            raise ZoomAutomationError("unsupported_platform")
        import comtypes.client

        # Frozen builds contain these modules; source runs may generate them once.
        try:
            api = import_module("comtypes.gen.UIAutomationClient")
        except ImportError:
            api = comtypes.client.GetModule("UIAutomationCore.dll")
        self.api = api
        self.errors = (OSError, ValueError, comtypes.COMError)
        self.client = comtypes.CoCreateInstance(
            api.CUIAutomation8._reg_clsid_,
            interface=api.IUIAutomation2,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        self.client.ConnectionTimeout = 2000
        self.client.TransactionTimeout = 2000
        self.cache = self.client.CreateCacheRequest()
        for property_id in (30005, 30003, 30012, 30001, 30010, 30022):
            self.cache.AddProperty(property_id)
        self.query_count = 0

    def _element(self, raw) -> Element:
        bounds = raw.CachedBoundingRectangle
        return Element(
            raw,
            raw.CachedName or "",
            raw.CachedControlType,
            raw.CachedClassName or "",
            (bounds.left, bounds.top, bounds.right, bounds.bottom),
        )

    def root(self, handle: int) -> Element:
        self.query_count += 1
        return self._element(self.client.ElementFromHandleBuildCache(handle, self.cache))

    def find_class(self, root: Element, class_name: str) -> Element | None:
        condition = self.client.CreatePropertyCondition(30012, class_name)
        self.query_count += 1
        found = root.raw.FindFirstBuildCache(4, condition, self.cache)
        return self._element(found) if found else None

    def elements(self, root: Element, *kinds: int) -> list[Element]:
        conditions = [self.client.CreatePropertyCondition(30003, kind) for kind in kinds]
        condition = self.client.CreateTrueCondition()
        if conditions:
            condition = conditions[0]
            for other in conditions[1:]:
                condition = self.client.CreateOrCondition(condition, other)
        self.query_count += 1
        found = root.raw.FindAllBuildCache(4, condition, self.cache)
        return [self._element(found.GetElement(index)) for index in range(found.Length)]

    def pattern(self, element: Element, pattern: int):
        interface = (
            self.api.IUIAutomationInvokePattern
            if pattern == INVOKE
            else self.api.IUIAutomationTogglePattern
        )
        try:
            raw = element.raw.GetCurrentPattern(pattern)
            return raw.QueryInterface(interface) if raw else None
        except self.errors:
            return None


class ZoomControls:
    """Actions stay bound to the window and process selected for the sequence."""

    def __init__(self, desktop: WindowsDesktop, automation: Automation):
        self.desktop, self.automation = desktop, automation
        self._step_deadline: float | None = None
        window = desktop.meeting()
        if window is None:
            raise ZoomAutomationError("meeting_not_found")
        self.window = window
        self._panel_window = self.window
        self.check_meeting()

    def begin_step(self) -> None:
        self._step_deadline = time.monotonic() + 8

    def check_meeting(self) -> None:
        if self._step_deadline is not None and time.monotonic() >= self._step_deadline:
            raise ZoomAutomationError("step_timeout")
        if not self.desktop.valid(self.window):
            raise ZoomAutomationError("meeting_not_found")

    def _wait(self, read: Callable, *, timeout: float = 5, code: str = "control_not_found"):
        deadline = time.monotonic() + timeout
        if self._step_deadline is not None:
            deadline = min(deadline, self._step_deadline)
        while True:
            self.check_meeting()
            try:
                value = read()
            except self.automation.errors:
                value = None
            if value:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ZoomAutomationError(code)
            time.sleep(min(0.08, remaining))

    def _root(self) -> Element:
        self.check_meeting()
        return self.automation.root(self.window.handle)

    def _toolbar(self) -> Element:
        root = self._root()
        if self.window.class_name == FLOATING_CLASS:
            return root
        panel = self.automation.find_class(root, TOOLBAR_CLASS)
        if panel is not None:
            return panel
        self.desktop.focus(self.window)
        self.desktop.hover(self.window)
        try:
            return self._wait(
                lambda: self.automation.find_class(self._root(), TOOLBAR_CLASS), timeout=0.5
            )
        except ZoomAutomationError as exc:
            if exc.code != "control_not_found":
                raise
        self.desktop.hover(self.window, real=True)
        return self._wait(
            lambda: self.automation.find_class(self._root(), TOOLBAR_CLASS),
            timeout=2,
            code="toolbar_not_found",
        )

    @staticmethod
    def _named(elements: list[Element], labels: list[str]) -> Element | None:
        matches = [
            (score, element)
            for element in elements
            if (score := _toolbar_action_match_score(element.name, labels)) > 0
        ]
        return max(matches, key=lambda item: item[0])[1] if matches else None

    def _buttons(self) -> list[Element]:
        return self.automation.elements(self._toolbar(), BUTTON)

    def _audio(self) -> Element:
        buttons = self._buttons()
        match = self._named(buttons, AUDIO_MUTED_TEXT + AUDIO_UNMUTED_TEXT + CONNECT_AUDIO_TEXT)
        # The first toolbar action is audio; unlike microphone/video state,
        # connection can also be distinguished by presence of its shortcut.
        if match:
            return match
        if buttons and len(buttons) > 1:
            return buttons[0]
        raise ZoomAutomationError("audio_control_not_found")

    def _audio_joined(self) -> bool:
        button = self._audio()
        state = _audio_button_state_from_text(button.name)
        if state != AudioState.UNKNOWN:
            return state != AudioState.DISCONNECTED
        return bool(_HOTKEY.search(button.name))

    def _video(self) -> Element:
        buttons = self._buttons()
        button = self._named(buttons, VIDEO_STARTED_TEXT + VIDEO_STOPPED_TEXT)
        if button:
            return button
        raise ZoomAutomationError("video_state_unknown")

    def _refresh_action(self, element: Element) -> Element:
        self.check_meeting()
        # Refresh geometry immediately before an action; windows may have moved
        # since discovery. A changed action label must never be toggled blindly.
        self.automation.query_count += 1
        refreshed = self.automation._element(element.raw.BuildUpdatedCache(self.automation.cache))
        if refreshed.name != element.name or not refreshed.raw.CachedIsEnabled:
            raise ZoomAutomationError("control_changed")
        return refreshed

    def _press(self, element: Element, window: Window | None = None) -> bool:
        element = self._refresh_action(element)
        try:
            pattern = self.automation.pattern(element, INVOKE)
            if pattern:
                pattern.Invoke()
                return True
        except self.automation.errors as exc:
            # An exception does not prove that the provider performed no action.
            # Repeating a toggle here could undo an action that already happened.
            hresult = getattr(exc, "hresult", None)
            if not isinstance(hresult, int) or hresult & 0xFFFFFFFF not in {
                0x80004001,  # E_NOTIMPL
                0x80040200,  # UIA_E_NOTSUPPORTED
            }:
                raise ZoomAutomationError("invoke_failed") from exc
            element = self._refresh_action(element)
        self.desktop.click(window or self.window, element.rect)
        return False

    def _popup(self, class_name: str, *, timeout: float = 3) -> tuple[Window, Element]:
        def read():
            windows = self.desktop.windows(class_name, pid=self.window.pid)
            if len(windows) != 1:
                return None
            return windows[0], self.automation.root(windows[0].handle)

        return self._wait(read, timeout=timeout, code="dialog_not_found")

    def _open_menu(self, element: Element, window: Window | None = None) -> tuple[Window, Element]:
        self.desktop.focus(window or self.window)
        invoked = self._press(element, window)
        try:
            return self._popup("WCN_ModelessWnd", timeout=0.4)
        except ZoomAutomationError as exc:
            if exc.code != "dialog_not_found":
                raise
        # Some Zoom menus advertise Invoke but silently ignore it.
        if invoked:
            refreshed = self._refresh_action(element)
            self.desktop.click(window or self.window, refreshed.rect)
        return self._popup("WCN_ModelessWnd")

    def join_audio(self) -> None:
        if self._audio_joined():
            return
        self.desktop.focus(self.window)
        self._press(self._audio())
        # Some clients connect immediately, others show the computer-audio dialog.
        pressed_dialog = False

        def connect():
            nonlocal pressed_dialog
            if self._audio_joined():
                return True
            dialogs = self.desktop.windows("zJoinAudioWndClass", pid=self.window.pid)
            if len(dialogs) == 1 and not pressed_dialog:
                dialog = dialogs[0]
                buttons = self.automation.elements(self.automation.root(dialog.handle), BUTTON)
                button = self._named(buttons, JOIN_AUDIO_TEXT)
                if button is None:
                    # Zoom's primary computer-audio action is the largest dialog button.
                    candidates = [b for b in buttons if b.name and b.rect[2] > b.rect[0]]
                    button = max(
                        candidates,
                        key=lambda b: (b.rect[2] - b.rect[0]) * (b.rect[3] - b.rect[1]),
                        default=None,
                    )
                if button is None:
                    raise ZoomAutomationError("join_audio_control_not_found")
                self._press(button, dialog)
                pressed_dialog = True
                return self._audio_joined()
            return False

        self._wait(connect, timeout=5, code="audio_not_connected")

    def leave_audio(self) -> None:
        if not self._audio_joined():
            return
        menus = self.automation.elements(self._toolbar(), MENUITEM)
        if not menus:
            raise ZoomAutomationError("audio_menu_not_found")
        self.desktop.focus(self.window)
        menu_window, menu = self._open_menu(menus[0])
        items = [
            el
            for el in self.automation.elements(menu, MENUITEM)
            if el.name and not _SEPARATOR.fullmatch(el.name.strip())
        ]
        button = self._named(items, LEAVE_AUDIO_TEXT)
        if button is None and len(items) >= 2:
            button = items[-2]
        if button is None:
            raise ZoomAutomationError("leave_audio_control_not_found")
        self._press(button, menu_window)
        self._wait(lambda: not self._audio_joined(), code="audio_not_disconnected")

    def set_microphone(self, enabled: bool) -> None:
        wanted = AudioState.UNMUTED if enabled else AudioState.MUTED
        self._set_media_state(
            self._audio, _audio_button_state_from_text, wanted, "microphone_not_changed"
        )

    def set_video(self, enabled: bool) -> None:
        wanted = VideoState.STARTED if enabled else VideoState.STOPPED
        self._set_media_state(
            self._video, _video_button_state_from_text, wanted, "video_not_changed"
        )

    def _set_media_state(
        self,
        find: Callable[[], Element],
        state: Callable[[str], AudioState | VideoState],
        wanted: AudioState | VideoState,
        code: str,
    ) -> None:
        # Zoom can accept Invoke while its media engine is transitioning and
        # silently discard it. Reconcile the desired state, never replay the
        # sequence or retry an uncertain COM failure. Each corrective dispatch
        # requires a fresh, recognized opposite state and refreshed geometry.
        def read():
            element = find()
            current = state(element.name)
            if current in (AudioState.UNKNOWN, AudioState.DISCONNECTED):
                raise ZoomAutomationError(
                    "audio_state_unknown" if isinstance(current, AudioState) else "video_state_unknown"
                )
            return element, current

        for attempt in range(3):
            _, current = read()
            if current == wanted:
                return
            self.desktop.focus(self.window)
            element, current = read()
            if current == wanted:
                return
            self._press(element)
            try:
                self._wait(
                    lambda: read()[1] == wanted,
                    timeout=1 if attempt < 2 else 5,
                    code=code,
                )
                return
            except ZoomAutomationError as exc:
                if exc.code != code or attempt == 2:
                    raise
                log.warning(
                    "Zoom media action had no observed effect: operation=%s target=%s attempt=%s",
                    code,
                    wanted,
                    attempt + 1,
                )

    def _participant_controls(self) -> list[Element]:
        if not self.desktop.valid(self._panel_window):
            self._panel_window = self.window
        root = self.automation.root(self._panel_window.handle)
        return self.automation.elements(root, BUTTON, SPLITBUTTON)

    def _mute_button(self, elements: list[Element]) -> Element | None:
        return next(
            (
                el
                for el in elements
                if el.kind == BUTTON and re.search(r"(?<![\w+])Alt\+M(?![\w+])", el.name)
            ),
            None,
        )

    def _participants(self) -> list[Element]:
        elements = self._participant_controls()
        if self._mute_button(elements):
            return elements
        # Detached participant panels are top-level windows in the same process.
        for window in self.desktop.windows(pid=self.window.pid):
            if window.handle == self.window.handle:
                continue
            detached = self.automation.elements(
                self.automation.root(window.handle), BUTTON, SPLITBUTTON
            )
            if self._mute_button(detached):
                self._panel_window = window
                return detached
        if self.automation.elements(self._root(), 50008):
            raise ZoomAutomationError("host_controls_not_found")
        button = self._named(self._buttons(), PARTICIPANTS_TEXT)
        if button is None:
            raise ZoomAutomationError("participants_control_not_found")
        self.desktop.focus(self.window)
        self._press(button)

        def opened():
            for window in [self.window, *self.desktop.windows(pid=self.window.pid)]:
                controls = self.automation.elements(
                    self.automation.root(window.handle), BUTTON, SPLITBUTTON
                )
                if self._mute_button(controls):
                    self._panel_window = window
                    return controls
            return None

        return self._wait(opened, code="host_controls_not_found")

    def mute_all(self, allow_unmute: bool) -> None:
        button = self._mute_button(self._participants())
        if button is None:
            raise ZoomAutomationError("host_controls_not_found")
        self.desktop.focus(self._panel_window)
        self._press(button, self._panel_window)
        dialog_window, dialog = self._popup("zChangeNameWndClass")
        checkboxes = self.automation.elements(dialog, CHECKBOX)
        if len(checkboxes) != 1:
            raise ZoomAutomationError("allow_unmute_control_not_found")
        checkbox = checkboxes[0]
        toggle = self.automation.pattern(checkbox, TOGGLE)
        if toggle is None:
            raise ZoomAutomationError("allow_unmute_state_unknown")
        wanted = int(allow_unmute)
        if toggle.CurrentToggleState != wanted:
            toggle.Toggle()
        self._wait(
            lambda: toggle.CurrentToggleState == wanted, timeout=1, code="allow_unmute_not_changed"
        )
        buttons = self.automation.elements(dialog, BUTTON)
        if not buttons:
            raise ZoomAutomationError("confirm_control_not_found")
        lowest = max(b.rect[1] for b in buttons)
        row = sorted([b for b in buttons if abs(b.rect[1] - lowest) <= 8], key=lambda b: b.rect[0])
        if len(row) != 2:
            raise ZoomAutomationError("confirm_control_not_found")
        self._press(row[0], dialog_window)
        self._wait(
            lambda: not self.desktop.windows("zChangeNameWndClass", pid=self.window.pid),
            code="mute_all_not_confirmed",
        )

    def ask_all_to_unmute(self) -> None:
        elements = self._participants()
        mute = self._mute_button(elements)
        index = elements.index(mute) if mute else len(elements)
        more = next((el for el in elements[index + 1 :] if el.kind == SPLITBUTTON), None)
        if more is None:
            raise ZoomAutomationError("participants_menu_not_found")
        menu_window, menu = self._open_menu(more, self._panel_window)
        group = []
        for el in self.automation.elements(menu, MENUITEM):
            if _SEPARATOR.fullmatch(el.name.strip()):
                if group:
                    break
            elif el.name:
                group.append(el)
        if not 1 <= len(group) <= 2 or self.automation.pattern(group[0], TOGGLE):
            raise ZoomAutomationError("ask_unmute_control_not_found")
        self._press(group[0], menu_window)
        self._wait(
            lambda: not self.desktop.windows("WCN_ModelessWnd", pid=self.window.pid),
            code="ask_unmute_not_sent",
        )

    def inspect(self) -> MeetingState:
        """Read without revealing controls, changing focus, or moving the cursor."""
        root = self._root()
        panel = (
            root
            if self.window.class_name == FLOATING_CLASS
            else self.automation.find_class(root, TOOLBAR_CLASS)
        )
        if panel is None:
            return MeetingState(in_meeting=True)
        buttons = self.automation.elements(panel, BUTTON)
        audio = self._named(buttons, AUDIO_MUTED_TEXT + AUDIO_UNMUTED_TEXT + CONNECT_AUDIO_TEXT)
        video = self._named(buttons, VIDEO_STARTED_TEXT + VIDEO_STOPPED_TEXT)
        return MeetingState(
            in_meeting=True,
            audio=_audio_button_state_from_text(audio.name) if audio else AudioState.UNKNOWN,
            video=_video_button_state_from_text(video.name) if video else VideoState.UNKNOWN,
        )
