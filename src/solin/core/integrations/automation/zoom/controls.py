"""Scoped native UI Automation queries and verified Zoom audio/video actions."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from solin.core.foundation.constants import IS_COMPILED

from .i18n_labels import (
    ALLOW_UNMUTE_TEXT,
    ASK_UNMUTE_TEXT,
    AUDIO_MENU_TEXT,
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    CONFIRM_MUTE_ALL_TEXT,
    CONNECT_AUDIO_TEXT,
    JOIN_AUDIO_TEXT,
    LEAVE_AUDIO_TEXT,
    MUTE_ALL_TEXT,
    PARTICIPANTS_LIST_TEXT,
    PARTICIPANTS_MORE_TEXT,
    PARTICIPANTS_TEXT,
    TOOLBAR_OVERFLOW_TEXT,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
)
from .native import FLOATING_CLASS, TOOLBAR_CLASS, Window, WindowsDesktop
from .state import AudioState, MeetingState, VideoState, ZoomAutomationError
from .text_match import (
    _audio_button_state_from_text,
    _grid_item_title,
    _label_match_score,
    _video_button_state_from_text,
)

BUTTON, CHECKBOX, MENUITEM, SPLITBUTTON, WINDOW = 50000, 50002, 50011, 50031, 50032
LISTITEM, LIST, TABITEM, TEXT = 50007, 50008, 50019, 50020
INVOKE, TOGGLE = 10000, 10015
_OVERFLOW_WINDOW_CLASS = "ZGridMultiLevelPopupWndClass"
_PARTICIPANT_WINDOW_CLASS = "ZPlistPopupContainerWndClass"
_AUDIO_TEXT = AUDIO_MUTED_TEXT + AUDIO_UNMUTED_TEXT + CONNECT_AUDIO_TEXT
_VIDEO_TEXT = VIDEO_STARTED_TEXT + VIDEO_STOPPED_TEXT
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
            if IS_COMPILED:
                raise
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

    def participant_rows(self, listing: Element) -> list[tuple[Element, tuple[str, ...]]]:
        # Zoom exposes the display name as a Text descendant, below intermediate
        # panes. Cache the rows and their descendants in one provider request.
        cache = self.cache.Clone()
        cache.TreeScope = 7  # TreeScope_Subtree
        condition = self.client.CreatePropertyCondition(30003, LISTITEM)
        self.query_count += 1
        rows = listing.raw.FindAllBuildCache(2, condition, cache)
        result = []
        for index in range(rows.Length):
            raw = rows.GetElement(index)
            names = tuple(
                child.CachedName
                for child in self._cached_descendants(raw)
                if child.CachedControlType == TEXT and child.CachedName
            )
            result.append((self._element(raw), names))
        return result

    @staticmethod
    def _cached_descendants(raw):
        pending = [raw]
        while pending:
            children = pending.pop().GetCachedChildren()
            if children:
                for index in range(children.Length):
                    child = children.GetElement(index)
                    yield child
                    pending.append(child)

    def compare(self, first: Element, second: Element) -> bool:
        return bool(self.client.CompareElements(first.raw, second.raw))

    def refresh(self, element: Element) -> Element:
        self.query_count += 1
        return self._element(element.raw.BuildUpdatedCache(self.cache))

    def outside_list(self, element: Element, root: Element) -> bool:
        walker = self.client.ControlViewWalker
        parent = element.raw
        for _ in range(32):
            self.query_count += 1
            parent = walker.GetParentElementBuildCache(parent, self.cache)
            if not parent:
                return False
            kind = parent.CachedControlType
            if kind in (LIST, LISTITEM):
                return False
            if kind == WINDOW:
                return bool(self.client.CompareElements(parent, root.raw))
        return False

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
    def _named(
        elements: list[Element], labels: list[str], *, decorated: bool = False
    ) -> Element | None:
        matches = [
            element
            for element in elements
            if _label_match_score(element.name, labels, decorated=decorated)
        ]
        return matches[0] if len(matches) == 1 else None

    def _buttons(self) -> list[Element]:
        return self.automation.elements(self._toolbar(), BUTTON)

    def _audio(self) -> Element:
        button = self._named(self._buttons(), _AUDIO_TEXT, decorated=True)
        if button:
            return button
        raise ZoomAutomationError("audio_control_not_found")

    def _read_audio_state(self, buttons: list[Element]) -> AudioState:
        button = self._named(buttons, _AUDIO_TEXT, decorated=True)
        return _audio_button_state_from_text(button.name) if button else AudioState.UNKNOWN

    def _video(self) -> Element:
        buttons = self._buttons()
        button = self._named(buttons, _VIDEO_TEXT, decorated=True)
        if button:
            return button
        raise ZoomAutomationError("video_state_unknown")

    def _refresh_action(self, element: Element) -> Element:
        self.check_meeting()
        # Refresh geometry immediately before an action; windows may have moved
        # since discovery. A changed action label must never be toggled blindly.
        refreshed = self.automation.refresh(element)
        if refreshed.name != element.name or not refreshed.raw.CachedIsEnabled:
            raise ZoomAutomationError("control_changed")
        return refreshed

    def _press(
        self, element: Element, window: Window | None = None, *, click_fallback: bool = True
    ) -> bool:
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
            if click_fallback:
                element = self._refresh_action(element)
        if click_fallback:
            self.desktop.click(window or self.window, element.rect)
        return False

    def _popup(self, class_name: str, *, timeout: float = 3) -> tuple[Window, Element]:
        def read():
            windows = self.desktop.windows(class_name, pid=self.window.pid)
            if len(windows) != 1:
                return None
            return windows[0], self.automation.root(windows[0].handle)

        return self._wait(read, timeout=timeout, code="dialog_not_found")

    def _open_menu(
        self,
        element: Element,
        window: Window | None = None,
        *,
        popup_class: str = "WCN_ModelessWnd",
    ) -> tuple[Window, Element]:
        self.desktop.focus(window or self.window)
        invoked = self._press(element, window)
        try:
            return self._popup(popup_class, timeout=0.5)
        except ZoomAutomationError as exc:
            if exc.code != "dialog_not_found":
                raise
        # Some Zoom menus advertise Invoke but silently ignore it.
        if invoked:
            refreshed = self._refresh_action(element)
            self.desktop.click(window or self.window, refreshed.rect)
        return self._popup(popup_class)

    def join_audio(self) -> None:
        if _audio_button_state_from_text(self._audio().name) != AudioState.DISCONNECTED:
            return
        self.desktop.focus(self.window)
        button = self._audio()
        if _audio_button_state_from_text(button.name) != AudioState.DISCONNECTED:
            return
        self._press(button)
        # Some clients connect immediately, others show the computer-audio dialog.
        pressed_dialog = False

        def connect():
            nonlocal pressed_dialog
            if self._read_audio_state(self._buttons()).is_connected:
                return True
            dialogs = self.desktop.windows("zJoinAudioWndClass", pid=self.window.pid)
            if len(dialogs) == 1 and not pressed_dialog:
                dialog = dialogs[0]
                buttons = self.automation.elements(self.automation.root(dialog.handle), BUTTON)
                button = self._named(buttons, JOIN_AUDIO_TEXT)
                if button is None:
                    raise ZoomAutomationError("join_audio_control_not_found")
                self._press(button, dialog)
                pressed_dialog = True
                return self._read_audio_state(self._buttons()).is_connected is True
            return False

        self._wait(connect, timeout=5, code="audio_not_connected")

    def leave_audio(self) -> None:
        if _audio_button_state_from_text(self._audio().name) == AudioState.DISCONNECTED:
            return
        menus = self.automation.elements(self._toolbar(), MENUITEM, SPLITBUTTON, BUTTON)
        audio_menu = self._named(menus, AUDIO_MENU_TEXT, decorated=True)
        if audio_menu is None:
            raise ZoomAutomationError("audio_menu_not_found")
        menu_window, menu = self._open_menu(audio_menu)
        items = self.automation.elements(menu, MENUITEM)
        button = self._named(items, LEAVE_AUDIO_TEXT)
        if button is None:
            raise ZoomAutomationError("leave_audio_control_not_found")
        self._press(button, menu_window)
        self._wait(
            lambda: self._read_audio_state(self._buttons()) == AudioState.DISCONNECTED,
            code="audio_not_disconnected",
        )

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
        return self.automation.elements(root, BUTTON, SPLITBUTTON, LIST)

    def _mute_button(self, elements: list[Element]) -> Element | None:
        return self._named(
            [el for el in elements if el.kind == BUTTON], MUTE_ALL_TEXT, decorated=True
        )

    @staticmethod
    def _has_participant_list(elements: list[Element]) -> bool:
        return any(
            el.kind == LIST and _label_match_score(el.name, PARTICIPANTS_LIST_TEXT)
            for el in elements
        )

    def _find_participant_panel(self, *, require_list: bool = False) -> list[Element] | None:
        def matches(elements: list[Element]) -> bool:
            return self._has_participant_list(elements) or (
                not require_list and self._mute_button(elements) is not None
            )

        if require_list:
            self._panel_window = self.window
            elements = self.automation.elements(self._root(), LIST)
            windows = (_PARTICIPANT_WINDOW_CLASS,)
            kinds = (LIST,)
        else:
            elements = self._participant_controls()
            windows = ()
            kinds = (BUTTON, SPLITBUTTON, LIST)
        if matches(elements):
            return elements
        # Detached participant panels are top-level windows in the same process.
        panels = []
        for window in self.desktop.windows(*windows, pid=self.window.pid):
            if window.handle == self._panel_window.handle:
                continue
            detached = self.automation.elements(self.automation.root(window.handle), *kinds)
            if matches(detached):
                panels.append((window, detached))
        if len(panels) > 1:
            raise ZoomAutomationError("participants_panel_ambiguous")
        if panels:
            self._panel_window, elements = panels[0]
            return elements
        return None

    def participant_list(self, *, open_if_closed: bool = False) -> tuple[Window, Element] | None:
        panel = self._find_participant_panel(require_list=True)
        if panel is None:
            if not open_if_closed:
                return None
            panel = self._open_participants_panel(require_list=True)
        listing = self._named(panel, PARTICIPANTS_LIST_TEXT)
        if listing is None:
            raise ZoomAutomationError("participants_list_ambiguous")
        if listing.raw.CachedIsOffscreen:
            raise ZoomAutomationError("participants_list_unavailable")
        return self._panel_window, listing

    def _participants_button(self) -> Element | None:
        return self._named(self._buttons(), PARTICIPANTS_TEXT, decorated=True)

    def _select_participants_from_overflow(self) -> None:
        if self.desktop.windows(_OVERFLOW_WINDOW_CLASS, pid=self.window.pid):
            window, menu = self._popup(_OVERFLOW_WINDOW_CLASS)
        else:
            buttons = self.automation.elements(self._toolbar(), MENUITEM, BUTTON, SPLITBUTTON)
            more = self._named(buttons, TOOLBAR_OVERFLOW_TEXT)
            if more is None:
                raise ZoomAutomationError("participants_control_not_found")
            window, menu = self._open_menu(more, popup_class=_OVERFLOW_WINDOW_CLASS)
        items = [
            item
            for item in self.automation.elements(menu, TABITEM)
            if _label_match_score(_grid_item_title(item.name), PARTICIPANTS_TEXT)
        ]
        if len(items) != 1:
            raise ZoomAutomationError("participants_control_not_found")
        self._press(items[0], window)

    def _open_participants_panel(self, *, require_list: bool = False) -> list[Element]:
        def find_panel():
            return self._find_participant_panel(require_list=require_list)

        self.desktop.focus(self.window)
        button = self._participants_button()
        if button is None:
            self._select_participants_from_overflow()
            # Some Zoom layouts move the selected item onto the toolbar.
            available = self._wait(
                lambda: find_panel() or self._participants_button(),
                timeout=2,
                code="participants_control_not_found",
            )
            if isinstance(available, list):
                return available
            button = available

        if panel := find_panel():
            return panel
        invoked = self._press(button)
        code = "participants_panel_not_opened"
        if invoked:
            try:
                return self._wait(find_panel, timeout=0.5, code=code)
            except ZoomAutomationError as exc:
                if exc.code != code:
                    raise
            # The accessible name includes a participant count that can change while waiting.
            retry_button = self._participants_button()
            if panel := find_panel():
                return panel
            if retry_button is not None:
                log.warning("Zoom Participants Invoke did not open the panel; retrying Invoke")
                self._press(retry_button, click_fallback=False)
        return self._wait(find_panel, timeout=3, code=code)

    def _participants(self) -> list[Element]:
        panel = self._find_participant_panel() or self._open_participants_panel()
        if self._mute_button(panel):
            return panel

        def host_controls() -> list[Element] | None:
            panel = self._find_participant_panel()
            return panel if panel and self._mute_button(panel) else None

        return self._wait(host_controls, timeout=1, code="host_controls_not_found")

    def mute_all(self, allow_unmute: bool) -> None:
        button = self._mute_button(self._participants())
        if button is None:
            raise ZoomAutomationError("host_controls_not_found")
        self.desktop.focus(self._panel_window)
        self._press(button, self._panel_window)
        dialog_window, dialog = self._popup("zChangeNameWndClass")
        checkboxes = self.automation.elements(dialog, CHECKBOX)
        checkbox = self._named(checkboxes, ALLOW_UNMUTE_TEXT)
        if checkbox is None:
            raise ZoomAutomationError("allow_unmute_control_not_found")
        confirm = self._named(self.automation.elements(dialog, BUTTON), CONFIRM_MUTE_ALL_TEXT)
        if confirm is None:
            raise ZoomAutomationError("confirm_control_not_found")
        toggle = self.automation.pattern(checkbox, TOGGLE)
        if toggle is None:
            raise ZoomAutomationError("allow_unmute_state_unknown")
        wanted = int(allow_unmute)
        if toggle.CurrentToggleState != wanted:
            toggle.Toggle()
        self._wait(
            lambda: toggle.CurrentToggleState == wanted, timeout=1, code="allow_unmute_not_changed"
        )
        self._press(confirm, dialog_window)
        self._wait(
            lambda: not self.desktop.windows("zChangeNameWndClass", pid=self.window.pid),
            code="mute_all_not_confirmed",
        )

    def ask_all_to_unmute(self) -> None:
        elements = self._participants()
        more = self._named(elements, PARTICIPANTS_MORE_TEXT, decorated=True)
        if more is None:
            raise ZoomAutomationError("participants_menu_not_found")
        menu_window, menu = self._open_menu(more, self._panel_window)
        button = self._named(self.automation.elements(menu, MENUITEM), ASK_UNMUTE_TEXT)
        if button is None:
            raise ZoomAutomationError("ask_unmute_control_not_found")
        self._press(button, menu_window)
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
        video = self._named(buttons, _VIDEO_TEXT, decorated=True)
        return MeetingState(
            in_meeting=True,
            audio=self._read_audio_state(buttons),
            video=_video_button_state_from_text(video.name) if video else VideoState.UNKNOWN,
        )
