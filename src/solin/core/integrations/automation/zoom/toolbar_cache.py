from __future__ import annotations

import time
from typing import Any

# ── Cache de elementos da toolbar ──────────────────────────────
# Evita re-scan de descendants + legacy_properties() a cada operacao.
# Invalidado quando o handle da janela principal muda.

TOOLBAR_CACHE_RECHECK_SECONDS = 5.0
TOOLBAR_CORE_CONTROL_IDS = {
    "panel_toolbar",
    "btn_paticipants",
    "btn_muteAudio",
    "btn_audioMenu",
    "btn_muteVideo",
}

# Controles do painel de participantes — cache proativo
PARTICIPANTS_PANEL_CONTROL_IDS = {
    "mute_all_btn",
    "more_btn",
}


class ToolbarCache:
    __slots__ = ("_handle", "_elements", "_validated_at")

    def __init__(self):
        self._handle: int | None = None
        self._elements: dict = {}  # cid -> element wrapper
        self._validated_at: dict = {}  # cid -> monotonic timestamp

    @staticmethod
    def _is_alive(element) -> bool:
        try:
            element.window_text()
            rect = element.rectangle()
            return rect.width() > 0 and rect.height() > 0
        except Exception:  # noqa: BLE001 - stale UIA element probe boundary
            return False

    def _reset_for_handle(self, handle: int) -> None:
        self._handle = handle
        self._elements = {}
        self._validated_at = {}

    def get(self, win, handle: int, control_id: str) -> Any | None:
        if handle != self._handle:
            self._reset_for_handle(handle)
        el = self._elements.get(control_id)
        if el is not None:
            now = time.monotonic()
            last_check = self._validated_at.get(control_id, 0.0)
            if now - last_check < TOOLBAR_CACHE_RECHECK_SECONDS:
                return el
            if self._is_alive(el):
                self._validated_at[control_id] = now
                return el
            self._elements.pop(control_id, None)
            self._validated_at.pop(control_id, None)
        return None

    def get_multi(self, handle: int, control_ids: set) -> dict:
        if handle != self._handle:
            return {}
        result = {}
        now = time.monotonic()
        for cid in control_ids:
            el = self._elements.get(cid)
            if el is not None:
                last_check = self._validated_at.get(cid, 0.0)
                if now - last_check < TOOLBAR_CACHE_RECHECK_SECONDS:
                    result[cid] = el
                    continue
                if self._is_alive(el):
                    self._validated_at[cid] = now
                    result[cid] = el
                    continue
                self._elements.pop(cid, None)
                self._validated_at.pop(cid, None)
        return result

    def put(self, handle: int, control_id: str, element) -> None:
        if handle != self._handle:
            self._reset_for_handle(handle)
        self._elements[control_id] = element
        self._validated_at[control_id] = time.monotonic()

    def put_many(self, handle: int, elements: dict) -> None:
        if handle != self._handle:
            self._reset_for_handle(handle)
        self._elements.update(elements)
        now = time.monotonic()
        for cid in elements:
            self._validated_at[cid] = now

    def invalidate(self) -> None:
        self._handle = None
        self._elements = {}
        self._validated_at = {}

    def invalidate_control(self, control_id: str) -> None:
        self._elements.pop(control_id, None)
        self._validated_at.pop(control_id, None)
