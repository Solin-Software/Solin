"""Automatic keyboard shortcut dispatch for media projection events."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import shutil
import subprocess
import sys
from typing import Protocol

from PySide6.QtCore import (
    QObject,
    QTimer,
)

from .auto_key_actions import (
    AUTO_KEY_EVENTS,
    AutoKeyAction,
)

log = logging.getLogger(__name__)

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "meta": "meta",
    "cmd": "meta",
    "command": "meta",
    "win": "meta",
    "windows": "meta",
}


class AutoKeySettingsSource(Protocol):
    def is_enabled(self) -> bool: ...

    def actions(self) -> list[AutoKeyAction]: ...


class AutoKeyDispatcher(QObject):
    """Loads configured actions and sends their key sequences in order."""

    def __init__(self, settings: AutoKeySettingsSource, parent=None):
        super().__init__(parent)
        self._settings = settings

    def dispatch(self, event: str) -> None:
        if event not in AUTO_KEY_EVENTS:
            return
        if not self._settings.is_enabled():
            return
        actions = [
            action for action in self._settings.actions()
            if action.enabled and action.event == event
        ]
        for index, action in enumerate(actions):
            QTimer.singleShot(
                index * 160,
                lambda sequence=action.sequence: self._send(sequence),
            )

    def _send(self, sequence: str) -> None:
        try:
            send_key_sequence(sequence)
        except Exception:  # noqa: BLE001 - desktop automation worker boundary
            log.exception("Failed to send automatic key sequence: %s", sequence)


def send_key_sequence(sequence: str) -> bool:
    """Send one Qt-style key sequence such as Ctrl+Alt+1."""
    chords = [part.strip() for part in sequence.split(",", 1) if part.strip()][:1]
    if not chords:
        return False
    ok = True
    for chord in chords:
        ok = _send_chord(chord) and ok
    return ok


def _send_chord(chord: str) -> bool:
    modifiers, key = _parse_chord(chord)
    if not key:
        return False
    if sys.platform == "win32":
        return _send_chord_windows(modifiers, key)
    if sys.platform == "darwin":
        return _send_chord_macos(modifiers, key)
    return _send_chord_linux(modifiers, key)


def _parse_chord(chord: str) -> tuple[list[str], str]:
    raw_tokens = [t.strip() for t in chord.replace("++", "+Plus").split("+")]
    modifiers: list[str] = []
    key_parts: list[str] = []
    for token in raw_tokens:
        if not token:
            continue
        normalized = token.lower().replace(" ", "")
        modifier = _MODIFIER_ALIASES.get(normalized)
        if modifier:
            if modifier not in modifiers:
                modifiers.append(modifier)
        else:
            key_parts.append(token)
    return modifiers, "+".join(key_parts).strip()


_VK_NAMES = {
    "backspace": 0x08,
    "tab": 0x09,
    "clear": 0x0C,
    "return": 0x0D,
    "enter": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "plus": 0xBB,
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "backslash": 0xDC,
}

for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x70 + _i - 1

_WIN_MODIFIERS = {
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "meta": 0x5B,
}


def _vk_for_key(key: str) -> int:
    normalized = key.lower().replace(" ", "")
    if normalized in _VK_NAMES:
        return _VK_NAMES[normalized]
    if len(key) == 1:
        char = key.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            return ord(char)
        if sys.platform == "win32":
            vk = ctypes.windll.user32.VkKeyScanW(ord(key))
            if vk != -1:
                return vk & 0xFF
    return 0


def _send_chord_windows(modifiers: list[str], key: str) -> bool:
    vk = _vk_for_key(key)
    if not vk:
        log.warning("Unsupported automatic shortcut key on Windows: %s", key)
        return False

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    events: list[INPUT] = []

    def add(vk_code: int, flags: int = 0) -> None:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki = KEYBDINPUT(vk_code, 0, flags, 0, 0)
        events.append(inp)

    mod_vks = [_WIN_MODIFIERS[m] for m in modifiers if m in _WIN_MODIFIERS]
    for mod_vk in mod_vks:
        add(mod_vk)
    add(vk)
    add(vk, KEYEVENTF_KEYUP)
    for mod_vk in reversed(mod_vks):
        add(mod_vk, KEYEVENTF_KEYUP)

    arr = (INPUT * len(events))(*events)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        err = ctypes.get_last_error()
        log.warning(
            "SendInput sent %s/%s events for %s+%s (last_error=%s)",
            sent,
            len(events),
            "+".join(modifiers),
            key,
            err,
        )
        return False
    return True


_MAC_MODIFIERS = {
    "shift": "shift down",
    "ctrl": "control down",
    "alt": "option down",
    "meta": "command down",
}

_MAC_KEY_CODES = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pgup": 116,
    "pagedown": 121,
    "pgdown": 121,
}


def _send_chord_macos(modifiers: list[str], key: str) -> bool:
    using = [_MAC_MODIFIERS[m] for m in modifiers if m in _MAC_MODIFIERS]
    using_clause = ""
    if using:
        using_clause = " using {" + ", ".join(using) + "}"
    normalized = key.lower().replace(" ", "")
    if len(key) == 1 and normalized not in _MAC_KEY_CODES:
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        command = f'tell application "System Events" to keystroke "{escaped}"{using_clause}'
    else:
        code = _MAC_KEY_CODES.get(normalized)
        if code is None:
            return False
        command = f'tell application "System Events" to key code {code}{using_clause}'
    try:
        subprocess.run(
            ["osascript", "-e", command],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


_XDOTOOL_KEYS = {
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "meta": "super",
}

_LINUX_KEY_NAMES = {
    "return": "Return",
    "enter": "Return",
    "escape": "Escape",
    "esc": "Escape",
    "space": "space",
    "tab": "Tab",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "pageup": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "pgdown": "Page_Down",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "home": "Home",
    "end": "End",
    "plus": "plus",
    "minus": "minus",
}


def _send_chord_linux(modifiers: list[str], key: str) -> bool:
    xdotool = shutil.which("xdotool")
    if not xdotool:
        log.warning("xdotool not found; automatic keys are unavailable on this Linux session")
        return False
    normalized = key.lower().replace(" ", "")
    key_name = _LINUX_KEY_NAMES.get(normalized)
    if key_name is None:
        if len(key) == 1:
            key_name = key
        elif normalized.startswith("f") and normalized[1:].isdigit():
            key_name = normalized.upper()
        else:
            return False
    parts = [_XDOTOOL_KEYS[m] for m in modifiers if m in _XDOTOOL_KEYS]
    parts.append(key_name)
    env = os.environ.copy()
    try:
        result = subprocess.run(
            [xdotool, "key", "--clearmodifiers", "+".join(parts)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            env=env,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
