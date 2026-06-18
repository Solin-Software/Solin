"""
controls.py — Controles robustos do Zoom via Windows UIA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Funcoes publicas:
  Todas as operacoes recebem uma ZoomSession pertencente ao ZoomService.
  get_meeting_state(session)       → estado completo da reuniao
  toggle_audio(session)            → alterna mute/unmute do proprio audio
  mute_all(session, allow_unmute)  → muta todos os participantes
  leave_computer_audio(session)    → sai do audio do computador
  stop_screen_share(session)       → para compartilhamento ativo
  dump_descendants(win)     → debug: lista descendentes de uma janela
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

from .i18n_labels import (
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    CONNECT_AUDIO_TEXT,
    PARTICIPANTS_TEXT,
    PEOPLE_SEPARATORS,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
    _JOIN_AUDIO_BTN_PATTERNS,
    _LEAVE_AUDIO_PATTERNS,
)
from .toolbar_cache import (
    PARTICIPANTS_PANEL_CONTROL_IDS,
    TOOLBAR_CORE_CONTROL_IDS,
    ToolbarCache,
)
from .text_match import (
    _audio_button_state_from_text,
    _matches_any,
    _toolbar_action_match_score,
    _video_button_state_from_text,
)
from .state import AudioState, MeetingState, ShareState, VideoState

log = logging.getLogger(__name__)


def _debug_ignored(message: str) -> None:
    log.debug(message, exc_info=True)


try:
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
    _PYWINAUTO_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    Desktop = None  # type: ignore[assignment]
    _PYWINAUTO_IMPORT_ERROR = exc

    def send_keys(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Zoom automation requires pywinauto on Windows.") from (
            _PYWINAUTO_IMPORT_ERROR
        )


def _require_pywinauto() -> None:
    if _PYWINAUTO_IMPORT_ERROR is not None:
        raise RuntimeError("Zoom automation requires pywinauto on Windows.") from (
            _PYWINAUTO_IMPORT_ERROR
        )

# ─────────────────────────────────────────────────────────────────
#  Timeouts
# ─────────────────────────────────────────────────────────────────

POPUP_TIMEOUT:  float = 5.0    # segundos — aguardar popup/janela aparecer
POLL_INTERVAL:  float = 0.03   # segundos — poll agressivo para resposta rapida


# ─────────────────────────────────────────────────────────────────
#  Constantes de janelas Zoom
# ─────────────────────────────────────────────────────────────────

ZOOM_WINDOW_CLASS_NAMES: set[str] = {
    "ConfMultiTabContentWndClass",
    "WCN_ModelessWnd",
    "zChangeNameWndClass",
    "ZGridMultiLevelPopupWndClass",
    "zJoinAudioWndClass",
    "ZPFloatToolbarClass",
    "ZPShareEntranceClass",
    "ZPMeetingWndClass",
    "ZPControlPanelClass",
}

TITLE_BLACKLIST: list[str] = [
    "chrome", "firefox", "mozilla", "edge", "opera", "safari", "brave",
    "vivaldi", "internet explorer", "tor browser",
    "slack", "teams", "discord", "skype",
    "outlook", "thunderbird",
    "vscode", "visual studio", "pycharm", "intellij", "webstorm",
    "notepad", "sublime", "obs ", "obs studio", "spotify", "vlc",
]

def _extract_control_id(element) -> Optional[str]:
    try:
        help_text = element.legacy_properties().get("Help", "")
        if help_text and help_text.startswith("{"):
            return json.loads(help_text).get("controlID")
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to extract Zoom controlID from UIA element")
    return None

def _is_blacklisted(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in TITLE_BLACKLIST)


def _poll(fn, timeout: float = POPUP_TIMEOUT, interval: float = POLL_INTERVAL):
    """
    Chama fn() repetidamente ate retornar valor truthy ou timeout expirar.
    Excecoes dentro de fn() sao ignoradas (tratadas como None).
    Retorna o ultimo resultado — truthy se encontrou, falsy/None se expirou.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = fn()
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            result = None
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


# ─────────────────────────────────────────────────────────────────
#  Descoberta de janelas Zoom
# ─────────────────────────────────────────────────────────────────

def _query_process_image_path(session: ZoomSession, pid: int) -> Optional[str]:
    """Retorna o caminho do executavel do processo, com cache curto em memoria."""
    if pid in session.process_image_cache:
        return session.process_image_cache[pid]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        session.process_image_cache[pid] = None
        return None

    try:
        size = ctypes.c_ulong(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            path = buf.value
        else:
            path = None
    finally:
        kernel32.CloseHandle(handle)

    session.process_image_cache[pid] = path
    return path


def _is_zoom_process_id(session: ZoomSession, pid: int) -> bool:
    path = _query_process_image_path(session, pid)
    if not path:
        return False
    return path.lower().endswith("\\zoom.exe")


def _find_zoom_windows_fast(session: ZoomSession) -> tuple[list, set]:
    """Descobre janelas Zoom via top-level scan. Retorna (janelas, pids)."""
    session.ensure_active()
    desktop = session.desktop()
    found: dict = {}
    pids:  set  = set()
    try:
        for w in desktop.windows():
            try:
                handle     = w.handle
                title      = w.window_text() or ""
                class_name = w.class_name() or ""
                pid        = w.process_id()
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
            title_l = title.lower()
            is_zoom_title = title_l in {"zoom", "zoom workplace"} or title_l.startswith("zoom ")
            is_zoom = (
                class_name in ZOOM_WINDOW_CLASS_NAMES
                or _is_zoom_process_id(session, pid)
                or is_zoom_title
            )
            if is_zoom and not _is_blacklisted(title):
                pids.add(pid)
                found[handle] = {
                    "handle": handle, "title": title,
                    "class_name": class_name, "pid": pid,
                }
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to enumerate Zoom windows")
    return list(found.values()), pids


def _get_window(session: ZoomSession, handle: int):
    session.ensure_active()
    return session.desktop().window(handle=handle)


def _find_main_window(
    session: ZoomSession,
    windows: list,
) -> tuple[Optional[dict], bool]:
    """
    Retorna (janela_principal, is_floating).
    is_floating=True quando esta em modo de compartilhamento (floating toolbar).
    """
    normal = next(
        (w for w in windows if w["class_name"] == "ConfMultiTabContentWndClass"), None
    )
    if normal:
        return normal, False
    floating = next(
        (w for w in windows if w["class_name"] == "ZPFloatToolbarClass"), None
    )
    if floating:
        return floating, True
    popup_classes = {
        "WCN_ModelessWnd",
        "zChangeNameWndClass",
        "ZGridMultiLevelPopupWndClass",
        "zJoinAudioWndClass",
        "ZPShareEntranceClass",
        "ZPControlPanelClass",
    }
    candidates = [w for w in windows if w.get("class_name") not in popup_classes]
    scored: list[tuple[int, dict]] = []
    for w in candidates:
        score = 0
        class_name = w.get("class_name", "")
        title = (w.get("title") or "").lower()
        if class_name.startswith("Conf"):
            score += 100
        if "meeting" in title or "reuni" in title:
            score += 20
        try:
            win = _get_window(session, w["handle"])
            for d in win.descendants():
                try:
                    d_class = d.class_name() or ""
                    if d_class == "ZPControlPanelClass":
                        score += 80
                        break
                    if d_class in {"VideoRenderWndClass", "VideoContainerWndClass"}:
                        score += 40
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to score candidate Zoom window")
        if score:
            scored.append((score, w))
    if scored:
        return max(scored, key=lambda item: item[0])[1], False
    return None, False


def _wait_for_window_class(
    session: ZoomSession,
    class_name: str,
    timeout: float = POPUP_TIMEOUT,
) -> Optional[dict]:
    """
    Aguarda ate uma janela com a classe especificada aparecer.
    Poll a cada POLL_INTERVAL por ate timeout segundos.
    """
    from pywinauto import findwindows

    def _try() -> Optional[dict]:
        windows, pids = _find_zoom_windows_fast(session)
        for w in windows:
            if w["class_name"] == class_name:
                return w
        if pids:
            try:
                for el in findwindows.find_elements(
                    class_name=class_name, backend="uia", top_level_only=False
                ):
                    if el.process_id in pids and el.handle:
                        return {
                            "handle": el.handle, "title": el.name or "",
                            "class_name": class_name, "pid": el.process_id,
                        }
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                _debug_ignored("Failed to search Zoom popup by class")
        return None

    return _poll(_try, timeout=timeout)


# ─────────────────────────────────────────────────────────────────
#  Busca de elementos UIA
# ─────────────────────────────────────────────────────────────────

def _find_element_by_cid(
    session: ZoomSession,
    win,
    control_id: str,
    timeout: float = 0.0,
    *,
    use_cache: bool = False,
    cache_handle: int = 0,
) -> Any | None:
    """
    Busca elemento pelo controlID dentro de win.
    Com timeout=0 faz uma unica tentativa; caso contrario faz poll ate timeout.
    use_cache=True tenta o cache da toolbar antes do scan.
    """
    if use_cache and cache_handle:
        cached = session.toolbar_cache.get(win, cache_handle, control_id)
        if cached is not None:
            return cached

    def _try():
        try:
            for d in win.descendants():
                try:
                    if _extract_control_id(d) == control_id:
                        if use_cache and cache_handle:
                            session.toolbar_cache.put(cache_handle, control_id, d)
                        return d
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to scan Zoom descendants by controlID")
        return _find_structural_fallback(win, control_id)

    if timeout <= 0.0:
        return _try()
    return _poll(_try, timeout=timeout)


def _find_elements_by_cid(
    session: ZoomSession,
    win,
    control_ids: set[str],
    *,
    use_cache: bool = False,
    cache_handle: int = 0,
) -> dict[str, Any]:
    """Busca multiplos controlIDs de uma vez. Retorna {cid: element}."""
    result: dict[str, Any] = {}
    remaining = set(control_ids)

    # Tenta cache primeiro
    if use_cache and cache_handle:
        cached = session.toolbar_cache.get_multi(cache_handle, control_ids)
        if cached:
            result.update(cached)
            remaining -= set(cached.keys())
            if not remaining:
                return result

    try:
        for d in win.descendants():
            try:
                cid = _extract_control_id(d)
                if cid is not None and cid in remaining and cid not in result:
                    result[cid] = d
                    if use_cache and cache_handle:
                        session.toolbar_cache.put(cache_handle, cid, d)
                    if len(result) == len(control_ids):
                        break
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to scan Zoom descendants by controlID set")
    missing = remaining - set(result)
    for cid in missing:
        fallback = _find_structural_fallback(win, cid)
        if fallback is not None:
            result[cid] = fallback
            if use_cache and cache_handle:
                session.toolbar_cache.put(cache_handle, cid, fallback)
    return result


def _rect_is_visible(element) -> bool:
    try:
        rect = element.rectangle()
        return rect.width() > 0 and rect.height() > 0
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return False


def _rect_inside(inner, outer, *, tolerance: int = 3) -> bool:
    return (
        inner.left >= outer.left - tolerance
        and inner.right <= outer.right + tolerance
        and inner.top >= outer.top - tolerance
        and inner.bottom <= outer.bottom + tolerance
    )


def _find_toolbar_panel(win) -> Any | None:
    """Zoom 7 removed toolbar controlIDs, but kept ZPControlPanelClass."""
    panels = []
    try:
        for d in win.descendants():
            try:
                if d.class_name() == "ZPControlPanelClass" and _rect_is_visible(d):
                    panels.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to find Zoom toolbar panel")
    if panels:
        return max(panels, key=lambda p: p.rectangle().bottom)
    return None


def _toolbar_items_by_type(win, friendly_class_name: str) -> list:
    panel = _find_toolbar_panel(win)
    if not panel:
        return []
    panel_rect = panel.rectangle()
    items = []
    try:
        for d in panel.descendants():
            try:
                if d.friendly_class_name() != friendly_class_name:
                    continue
                rect = d.rectangle()
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                if _rect_inside(rect, panel_rect):
                    items.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return []
    return sorted(items, key=lambda item: (item.rectangle().left, item.rectangle().top))


def _button_has_numeric_badge(button) -> bool:
    try:
        for child in button.descendants():
            try:
                if child.friendly_class_name() == "Static":
                    text = (child.window_text() or "").strip()
                    if text.isdigit():
                        return True
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to inspect Zoom participant badge")
    return False


def _find_toolbar_button_by_action(
    buttons: list[Any],
    patterns: list[str],
) -> Any | None:
    best = None
    best_score = 0
    for button in buttons:
        try:
            score = _toolbar_action_match_score(button.window_text() or "", patterns)
            if score > best_score:
                best = button
                best_score = score
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            continue
    return best


def _find_toolbar_fallback(win, control_id: str) -> Any | None:
    """
    Zoom 7.0.2 no longer exposes the old JSON controlID in Legacy Help.
    These fallbacks use the stable toolbar structure/geometry observed in v7,
    while keeping v6 on the primary controlID path.
    """
    panel = _find_toolbar_panel(win)
    if not panel:
        return None
    if control_id == "panel_toolbar":
        return panel

    buttons = _toolbar_items_by_type(win, "Button")
    menu_items = _toolbar_items_by_type(win, "MenuItem")

    if control_id == "btn_muteAudio":
        by_text = _find_toolbar_button_by_action(
            buttons,
            AUDIO_MUTED_TEXT + AUDIO_UNMUTED_TEXT + CONNECT_AUDIO_TEXT,
        )
        if by_text:
            return by_text
        return buttons[0] if len(buttons) >= 1 else None
    if control_id == "btn_muteVideo":
        by_text = _find_toolbar_button_by_action(
            buttons,
            VIDEO_STOPPED_TEXT + VIDEO_STARTED_TEXT,
        )
        if by_text:
            return by_text
        return buttons[1] if len(buttons) >= 2 else None
    if control_id == "btn_paticipants":
        for button in buttons:
            if _button_has_numeric_badge(button):
                return button
        by_text = _find_toolbar_button_by_action(buttons, PARTICIPANTS_TEXT)
        if by_text:
            return by_text
        return buttons[4] if len(buttons) >= 5 else None
    if control_id == "btn_audioMenu":
        return menu_items[0] if menu_items else None
    if control_id == "btn_more":
        return menu_items[-1] if menu_items else None
    return None


def _find_participants_fallback(win, control_id: str) -> Any | None:
    listboxes = []
    try:
        for d in win.descendants():
            try:
                if d.friendly_class_name() == "ListBox" and _rect_is_visible(d):
                    listboxes.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return None

    if control_id == "unified_primary_list":
        if not listboxes:
            return None
        # The participants list is the right-side visible list with ListItem children.
        for box in sorted(listboxes, key=lambda b: (b.rectangle().left, b.rectangle().top), reverse=True):
            try:
                if any(c.friendly_class_name() == "ListItem" for c in box.descendants()):
                    return box
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
        return listboxes[-1]

    list_el = _find_participants_fallback(win, "unified_primary_list")
    if not list_el:
        return None
    list_rect = list_el.rectangle()
    footer_items = []
    wanted_type = "SplitButton" if control_id == "more_btn" else "Button"
    try:
        for d in win.descendants():
            try:
                if d.friendly_class_name() != wanted_type or not _rect_is_visible(d):
                    continue
                rect = d.rectangle()
                if rect.top >= list_rect.bottom - 5 and rect.left >= list_rect.left - 5:
                    footer_items.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return None
    footer_items.sort(key=lambda item: item.rectangle().left)

    if control_id == "mute_all_btn":
        return footer_items[1] if len(footer_items) >= 2 else (footer_items[0] if footer_items else None)
    if control_id == "more_btn":
        return footer_items[-1] if footer_items else None
    return None


def _find_confirmation_dialog_fallback(win, control_id: str) -> Any | None:
    """
    Zoom 7.0.2 removed chk_option/btn_rename from mute-all confirmation.
    In that dialog the checkbox is the only visible CheckBox and the confirm
    button is the leftmost normal-sized Button on the bottom action row.
    """
    if control_id not in {"chk_option", "btn_rename"}:
        return None

    try:
        class_name = win.class_name() or ""
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return None
    if class_name not in {"zChangeNameWndClass", "ZGridMultiLevelPopupWndClass", "WCN_ModelessWnd"}:
        return None

    try:
        win_rect = win.rectangle()
        descendants = win.descendants()
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return None

    if control_id == "chk_option":
        checkboxes = []
        for d in descendants:
            try:
                if d.friendly_class_name() == "CheckBox" and _rect_is_visible(d):
                    rect = d.rectangle()
                    if _rect_inside(rect, win_rect, tolerance=12):
                        checkboxes.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
        checkboxes.sort(key=lambda item: (item.rectangle().top, item.rectangle().left))
        return checkboxes[0] if checkboxes else None

    buttons = []
    for d in descendants:
        try:
            if d.friendly_class_name() != "Button" or not _rect_is_visible(d):
                continue
            rect = d.rectangle()
            if not _rect_inside(rect, win_rect, tolerance=12):
                continue
            # Exclude title-bar close/minimize buttons; keep bottom action buttons.
            if rect.top < win_rect.top + (win_rect.height() * 0.55):
                continue
            if rect.width() < 35 or rect.height() < 24:
                continue
            buttons.append(d)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            continue
    buttons.sort(key=lambda item: (item.rectangle().top, item.rectangle().left))
    return buttons[0] if buttons else None


def _find_structural_fallback(win, control_id: str) -> Any | None:
    return (
        _find_toolbar_fallback(win, control_id)
        or _find_participants_fallback(win, control_id)
        or _find_confirmation_dialog_fallback(win, control_id)
    )


def _visible_control_ids(
    session: ZoomSession,
    win,
    handle: int,
    control_ids: set[str],
) -> dict[str, Any]:
    result = _find_elements_by_cid(
        session,
        win,
        set(control_ids),
        use_cache=True,
        cache_handle=handle,
    )
    missing = set(control_ids) - set(result)
    for cid in missing:
        fallback = _find_structural_fallback(win, cid)
        if fallback is not None:
            result[cid] = fallback
            session.toolbar_cache.put(handle, cid, fallback)
    return result


def _participants_panel_visible(win) -> bool:
    """
    Detecção robusta se o painel de participantes está aberto.

    NÃO usa cache — a visibilidade do painel é volátil (o usuário pode
    abrir/fechar a qualquer momento). Sempre faz verificação estrutural
    fresca com validação de bounds para máxima confiabilidade.
    """
    try:
        win_rect = win.rectangle()
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return False

    # Busca estrutural: ListBox visível, DENTRO da janela, com ListItem children
    try:
        for d in win.descendants():
            try:
                if d.friendly_class_name() != "ListBox":
                    continue
                rect = d.rectangle()
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                # Bounds check: deve estar dentro da janela principal.
                # Quando o painel é fechado, o Zoom pode manter o ListBox
                # no tree mas fora dos bounds visíveis.
                if not _rect_inside(rect, win_rect, tolerance=10):
                    continue
                # Confirma que é a lista de participantes (tem ListItem children)
                if any(c.friendly_class_name() == "ListItem" for c in d.descendants()):
                    return True
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to determine whether Zoom participants panel is visible")

    return False


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _move_mouse_to_reveal_toolbar(session: ZoomSession, win) -> None:
    """Revela a toolbar do Zoom 7 movendo o cursor para a base e restaurando."""
    def _move() -> None:
        pt = _POINT()
        user32 = ctypes.windll.user32
        user32.GetCursorPos(ctypes.byref(pt))
        try:
            rect = win.rectangle()
            x = (rect.left + rect.right) // 2
            y = max(rect.top, rect.bottom - 35)
            user32.SetCursorPos(x, y)
            time.sleep(0.12)
        finally:
            user32.SetCursorPos(pt.x, pt.y)

    session.perform_action(_move)


def _send_alt_to_reveal_toolbar(session: ZoomSession, win) -> None:
    def _send() -> None:
        try:
            win.set_focus()
            time.sleep(0.05)
            win.type_keys("%")
            return
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to reveal Zoom toolbar with window focus")
        send_keys("%")

    session.perform_action(_send)


def _find_element_by_text(
    win,
    patterns: list[str],
    timeout: float = 0.0,
) -> Any | None:
    """
    Busca elemento pelo texto (util para floating toolbar quando controlIDs
    nao estao disponiveis).
    """
    def _try():
        try:
            for d in win.descendants():
                try:
                    title = d.window_text() or ""
                    if _matches_any(title, patterns):
                        return d
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to find Zoom element by text")
        return None

    if timeout <= 0.0:
        return _try()
    return _poll(_try, timeout=timeout)


def _safe_invoke(
    session: ZoomSession,
    element,
    fallback_hwnd: Optional[int] = None,
) -> bool:
    """
    Invoca elemento clicável de forma invisível.
    Tenta as vias normais do UIA (invoke/toggle/select) pois controles padrões
    (como MenuItems) suportam nativamente.
    Se falhar (ex: botões flutuantes customizados do Zoom que apenas simulam suporte),
    tenta o clique via PostMessageW (se fallback_hwnd providenciado),
    pois revelou-se imune às blindagens do UIA sem mover o cursor físico.
    """
    def _invoke() -> bool:
        for method in ("invoke", "toggle", "select"):
            try:
                getattr(element, method)()
                return True
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue

        if fallback_hwnd:
            try:
                rect = element.rectangle()
                x_screen = (rect.left + rect.right) // 2
                y_screen = (rect.top + rect.bottom) // 2

                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                pt = POINT(x_screen, y_screen)
                ctypes.windll.user32.ScreenToClient(fallback_hwnd, ctypes.byref(pt))

                lparam = (pt.y << 16) | (pt.x & 0xFFFF)
                ctypes.windll.user32.PostMessageW(
                    fallback_hwnd, 0x0201, 1, lparam
                )
                time.sleep(0.01)
                ctypes.windll.user32.PostMessageW(
                    fallback_hwnd, 0x0202, 0, lparam
                )
                return True
            except Exception as exc:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                log.warning("PostMessage click failed: %s", exc)

        return False

    return bool(session.perform_action(_invoke))


def _click_in_popup(
    session: ZoomSession,
    popup_win,
    patterns: list[str],
) -> bool:
    """
    Encontra e invoca o primeiro item do popup cujo texto bate com patterns.
    Interacao programatica (invoke/toggle/select), sem clique de mouse.
    """
    try:
        for d in popup_win.descendants():
            try:
                title = d.window_text() or ""
                if _matches_any(title, patterns):
                    _safe_invoke(session, d)
                    time.sleep(0.1)
                    return True
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to click Zoom popup item")
    return False


# ─────────────────────────────────────────────────────────────────
#  ZoomSession — estado UIA pertencente a uma geração do ZoomService
# ─────────────────────────────────────────────────────────────────

class ZoomSession:
    """Owns UIA wrappers and caches for one ZoomService generation."""

    def __init__(self) -> None:
        self._pids:         set            = set()
        self._main_handle:  Optional[int]  = None
        self._is_floating:  bool           = False
        self._last_scan:    float          = 0.0
        self._desktop_local = threading.local()
        self._cancelled = threading.Event()
        self._action_lock = threading.Lock()
        self.process_image_cache: dict[int, Optional[str]] = {}
        self.toolbar_cache = ToolbarCache()
        self.stop_share_cache = _StopShareCache()

    def ensure_active(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("Zoom automation session is no longer active")

    def cancel(self) -> None:
        with self._action_lock:
            self._cancelled.set()
            self._pids.clear()
            self._main_handle = None
            self._is_floating = False
            self.process_image_cache.clear()
            self.toolbar_cache.invalidate()
            self.stop_share_cache.invalidate()

    def perform_action(self, action: Callable[[], Any]) -> Any:
        with self._action_lock:
            self.ensure_active()
            return action()

    def desktop(self) -> Desktop:
        self.ensure_active()
        _require_pywinauto()
        desktop_factory = Desktop
        if desktop_factory is None:
            raise RuntimeError("Zoom automation requires pywinauto on Windows.")
        desktop = getattr(self._desktop_local, "desktop", None)
        if desktop is None:
            desktop = desktop_factory(backend="uia")
            self._desktop_local.desktop = desktop
        return desktop

    def refresh(self, force: bool = False) -> None:
        self.ensure_active()
        now = time.monotonic()
        if not force and self._pids and (now - self._last_scan < 5.0):
            if self._main_handle:
                # Validacao rapida via Win32 (sem COM/UIA)
                if ctypes.windll.user32.IsWindow(self._main_handle):
                    return

        windows, self._pids = _find_zoom_windows_fast(self)
        if not self._pids:
            self._main_handle = None
            self._is_floating = False
            return
        main, is_floating    = _find_main_window(self, windows)
        self._main_handle    = main["handle"] if main else None
        self._is_floating    = is_floating
        self._last_scan      = now

    @property
    def pids(self) -> set:
        self.refresh()
        return self._pids

    @property
    def main_handle(self) -> Optional[int]:
        self.refresh()
        return self._main_handle

    @property
    def is_floating(self) -> bool:
        """True quando Zoom esta em modo de compartilhamento (floating toolbar)."""
        self.refresh()
        return self._is_floating

    def get_main_window(self):
        h = self.main_handle
        if h is None:
            raise RuntimeError("Zoom meeting window not found")
        return _get_window(self, h)

    def ensure_toolbar(self, control_ids: Optional[set[str]] = None,
                       *, force_reveal: bool = False) -> None:
        """
        Garante que a toolbar ou os controles pedidos estejam visiveis.
        Se os participantes estao acessiveis mas a toolbar sumiu, envia Alt
        para acordar os controles e tenta novamente.
        """
        if self.is_floating:
            return  # floating toolbar esta sempre visivel

        h = self.main_handle
        if h is None:
            raise RuntimeError("Zoom meeting window not found")

        win = _get_window(self, h)
        required = set(control_ids or ())
        probe_ids = required or TOOLBAR_CORE_CONTROL_IDS

        def _has_required_control() -> bool:
            try:
                handle = self.main_handle
                if handle is None:
                    return False
                found = _visible_control_ids(self, win, handle, probe_ids)
                if required:
                    return any(cid in found for cid in required)
                return bool(found)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                return False

        # Verifica primeiro sem tocar no foco. Quando um controle especifico foi
        # pedido, panel_toolbar sozinho nao basta: o botao pode ter sumido.
        if not force_reveal and _has_required_control():
            return

        # Toolbar oculta/stale — envia Alt para revelar. Fazemos duas tentativas
        # porque o Zoom pode consumir o primeiro Alt enquanto muda foco entre
        # lista de participantes e janela principal.
        last_error: Exception | None = None
        for _ in range(2):
            try:
                _send_alt_to_reveal_toolbar(self, win)
            except Exception as e:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                last_error = e
                continue

            if _poll(_has_required_control, timeout=1.5):
                return

            try:
                _move_mouse_to_reveal_toolbar(self, win)
            except Exception as e:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                last_error = e
            if _poll(_has_required_control, timeout=1.0):
                return

            self.refresh(force=True)
            h = self.main_handle
            if h is None:
                break
            win = _get_window(self, h)

        if last_error is not None:
            raise RuntimeError(f"Could not send Alt to reveal toolbar: {last_error}")
        missing = ", ".join(sorted(required)) if required else "toolbar controls"
        panel_hint = "participants panel is visible" if _participants_panel_visible(win) else "participants panel not visible"
        raise RuntimeError(f"Toolbar controls did not appear after Alt ({missing}; {panel_hint})")

def _require_meeting(session: ZoomSession) -> ZoomSession:
    session.refresh()
    if not session.pids:
        raise RuntimeError("No Zoom meeting found. Is Zoom running in a meeting?")
    return session


def warm_toolbar_cache(
    session: ZoomSession,
    control_ids: Optional[set[str]] = None,
    *,
    reveal: bool = False,
    include_participants: bool = False,
) -> bool:
    """
    Revalida/aquece o cache dos controles da toolbar.
    reveal=False nunca envia Alt nem rouba foco; reveal=True acorda a toolbar.
    include_participants=True também cacheia mute_all_btn e more_btn.
    """
    ctx = _require_meeting(session)
    if ctx.is_floating:
        return True

    h = ctx.main_handle
    if h is None:
        return False
    win = ctx.get_main_window()
    ids = set(control_ids or TOOLBAR_CORE_CONTROL_IDS)

    # Cache proativo dos botões de participantes quando solicitado
    if include_participants and _participants_panel_visible(win):
        ids = ids | PARTICIPANTS_PANEL_CONTROL_IDS

    found = _visible_control_ids(session, win, h, ids)
    if found:
        return True
    if not reveal:
        return False
    ctx.ensure_toolbar(ids, force_reveal=True)
    ctx.refresh(force=True)
    h = ctx.main_handle
    if h is None:
        return False
    win = ctx.get_main_window()
    return bool(_visible_control_ids(session, win, h, ids))


def _find_toolbar_button(
    session: ZoomSession,
    control_id: str,
    timeout: float = 2.0,
    *,
    fresh: bool = False,
) -> Any | None:
    """
    Busca um botao da toolbar de forma robusta:
    cache/scan primeiro, Alt para revelar quando necessario, e retry.
    """
    ctx = _require_meeting(session)
    if fresh:
        session.toolbar_cache.invalidate()
        ctx.refresh(force=True)

    if ctx.is_floating:
        win = ctx.get_main_window()
        return _find_element_by_cid(session, win, control_id, timeout=timeout)

    ctx.ensure_toolbar({control_id})
    win = ctx.get_main_window()
    h = ctx.main_handle
    if h is None:
        return None

    btn = _find_element_by_cid(
        session,
        win,
        control_id,
        timeout=timeout,
        use_cache=not fresh, cache_handle=h,
    )
    if btn:
        return btn

    # Se o panel_toolbar estava stale ou o Zoom ocultou os botoes entre o scan
    # e o clique, acorda de novo e refaz a busca.
    ctx.ensure_toolbar({control_id}, force_reveal=True)
    ctx.refresh(force=True)
    win = ctx.get_main_window()
    h = ctx.main_handle
    if h is None:
        return None
    return _find_element_by_cid(
        session,
        win,
        control_id,
        timeout=max(timeout, 1.0),
        use_cache=True, cache_handle=h,
    )

def _invoke_toolbar_button(
    session: ZoomSession,
    control_id: str,
    timeout: float = 2.0,
) -> bool:
    """
    Invoca um botao da toolbar com estratégia cache-first.

    Fluxo otimizado:
      1. Cache hit → invoke direto (~0ms)
      2. Cache miss → scan único + fallback estrutural → invoke
      3. Se invoke falhou (elemento stale) → invalida cache, revela toolbar, retry
    """
    ctx = _require_meeting(session)
    btn = _find_toolbar_button(session, control_id, timeout=timeout)
    fallback_hwnd = ctx.main_handle
    if btn and _safe_invoke(session, btn, fallback_hwnd=fallback_hwnd):
        return True

    # Primeiro invoke falhou — elemento pode estar stale.
    # Invalida apenas o elemento específico, não o cache inteiro.
    h = ctx.main_handle
    if h:
        session.toolbar_cache.invalidate_control(control_id)

    if not ctx.is_floating:
        ctx.ensure_toolbar({control_id}, force_reveal=True)

    btn = _find_toolbar_button(
        session,
        control_id,
        timeout=max(timeout, 1.0),
        fresh=True,
    )
    ctx.refresh(force=True)
    fallback_hwnd = ctx.main_handle
    return bool(
        btn and _safe_invoke(session, btn, fallback_hwnd=fallback_hwnd)
    )


# ─────────────────────────────────────────────────────────────────
#  Deteccao de compartilhamento
# ─────────────────────────────────────────────────────────────────

class _StopShareCache:
    """
    Cache persistente para o botão 'Stop Share' da floating toolbar.
    Validação rápida do UIA elemento.
    """
    __slots__ = ("_float_handle", "_btn")

    def __init__(self):
        self._float_handle: Optional[int] = None
        self._btn = None

    def get(self) -> Any | None:
        if self._btn is None:
            return None
        try:
            text = self._btn.window_text() or ""
            if not text.strip():
                raise ValueError("Ghost button in cache (empty text)")
            rect = self._btn.rectangle()
            if rect.width() <= 0 or rect.height() <= 0:
                raise ValueError("Invisible button in cache")
            return self._btn
        except Exception as e:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            log.debug("[CACHE-POLLER] Invalid cache: %s", e)
            self._btn = None
            self._float_handle = None
            return None

    def put(self, float_handle: int, btn) -> None:
        self._float_handle = float_handle
        self._btn = btn

    def invalidate(self) -> None:
        self._float_handle = None
        self._btn = None

    @property
    def float_handle(self) -> Optional[int]:
        return self._float_handle

def _find_float_toolbar_hwnd() -> Optional[int]:
    """
    Retorna handle da floating toolbar usando FindWindowW (~0ms).
    Filtra toolbars ocultas ou em estado de pré-inicialização do Zoom.
    """
    hwnd = ctypes.windll.user32.FindWindowW("ZPFloatToolbarClass", None)
    if hwnd and ctypes.windll.user32.IsWindowVisible(hwnd):
        return hwnd
    return None


def _scan_stop_btn(session: ZoomSession, float_handle: int) -> Any | None:
    """
    Varre a floating toolbar buscando o ÚLTIMO botão (Button).
    Independente de idioma e sem controlID, o Stop Share é sempre o último.
    Exige que o botão esteja completamente renderizado (tem texto e tamanho).
    """
    try:
        win = _get_window(session, float_handle)
        last_button = None
        for d in win.descendants():
            try:
                if d.friendly_class_name() == "Button":
                    text = d.window_text() or ""
                    rect = d.rectangle()
                    # Garante que o Zoom ja popularizou as strings e coordenadas na UI
                    if text.strip() and rect.width() > 0 and rect.height() > 0:
                        last_button = d
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
        return last_button
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return None


def _is_sharing(session: ZoomSession) -> bool:
    """
    Verifica estado de compartilhamento via Win32 (~0ms) e mantem cache quente.
    """
    float_handle = _find_float_toolbar_hwnd()
    stop_cache = session.stop_share_cache

    if not float_handle:
        stop_cache.invalidate()
        return False

    cached = stop_cache.get()
    if cached and stop_cache.float_handle == float_handle:
        log.debug("[CACHE-POLLER] Warm and valid cache!")
        return True

    log.debug("[CACHE-POLLER] Cold/stale cache. Scanning toolbar...")
    btn = _scan_stop_btn(session, float_handle)
    if btn:
        stop_cache.put(float_handle, btn)
        log.debug("[CACHE-POLLER] Success! Button saved to cache.")
    else:
        log.debug("[CACHE-POLLER] Failed! Button not found in toolbar.")

    return True


# ─────────────────────────────────────────────────────────────────
#  Estado da reuniao
# ─────────────────────────────────────────────────────────────────

def get_meeting_state(session: ZoomSession) -> MeetingState:
    """Retorna o estado atual da reuniao (audio, video, sharing, participantes)."""
    ctx = _require_meeting(session)
    ctx.ensure_toolbar()
    win = ctx.get_main_window()

    sharing = ShareState.from_active(_is_sharing(session))
    audio = AudioState.UNKNOWN
    video = VideoState.UNKNOWN
    audio_title = ""
    video_title = ""
    participant_count = 0

    if ctx.is_floating:
        # Modo floating: busca audio/video por texto (controlIDs indisponiveis)
        try:
            for d in win.descendants():
                try:
                    title = d.window_text() or ""
                    if not title:
                        continue
                    if audio is AudioState.UNKNOWN:
                        has_audio_signature = _toolbar_action_match_score(
                            title,
                            AUDIO_MUTED_TEXT + AUDIO_UNMUTED_TEXT + CONNECT_AUDIO_TEXT,
                        ) > 0
                        if has_audio_signature:
                            audio = _audio_button_state_from_text(title)
                            audio_title = title
                    if video is VideoState.UNKNOWN:
                        video_state = _video_button_state_from_text(title)
                        if video_state is not VideoState.UNKNOWN:
                            video = video_state
                            video_title = title
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to infer Zoom state from visible toolbar controls")
        return MeetingState(
            audio=audio,
            video=video,
            sharing=sharing,
            audio_title=audio_title,
            video_title=video_title,
            in_meeting=True,
        )

    # Modo normal: busca por controlID
    h = ctx.main_handle
    if h is None:
        return MeetingState(sharing=sharing, in_meeting=True)
    elements = _find_elements_by_cid(
        session,
        win,
        {"btn_muteAudio", "btn_muteVideo", "btn_paticipants"},
        use_cache=True, cache_handle=h,
    )

    audio_el = elements.get("btn_muteAudio")
    if audio_el:
        try:
            audio_title = audio_el.window_text() or ""
            audio = _audio_button_state_from_text(audio_title)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to read Zoom audio button state")

    video_el = elements.get("btn_muteVideo")
    if video_el:
        try:
            video_title = video_el.window_text() or ""
            video = _video_button_state_from_text(video_title)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to read Zoom video button state")

    part_el = elements.get("btn_paticipants")
    if part_el:
        try:
            title = part_el.window_text() or ""
            m = re.search(r"(\d+)", title)
            if m:
                participant_count = int(m.group(1))
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to read Zoom participant count from toolbar")

    return MeetingState(
        audio=audio,
        video=video,
        sharing=sharing,
        audio_title=audio_title,
        video_title=video_title,
        participant_count=participant_count,
        in_meeting=True,
    )


# ─────────────────────────────────────────────────────────────────
#  Audio: mute / unmute / toggle
# ─────────────────────────────────────────────────────────────────

def _get_audio_button(session: ZoomSession):
    """Retorna o elemento btn_muteAudio (ou equivalente na floating toolbar)."""
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    el = _find_toolbar_button(session, "btn_muteAudio", timeout=2.0)
    if not el and ctx.is_floating:
        el = _find_element_by_text(
            win,
            AUDIO_UNMUTED_TEXT + AUDIO_MUTED_TEXT + CONNECT_AUDIO_TEXT,
            timeout=2.0,
        )
    if not el:
        raise RuntimeError("btn_muteAudio not found")
    return el


def _audio_is_muted(session: ZoomSession) -> bool:
    el = _find_toolbar_button(
        session,
        "btn_muteAudio",
        timeout=2.0,
        fresh=True,
    ) or _get_audio_button(session)
    title = el.window_text() or ""
    state = _audio_button_state_from_text(title)
    if state == AudioState.UNMUTED:
        return False
    if state in {AudioState.MUTED, AudioState.DISCONNECTED}:
        return True
    return True  # fallback conservador: assume mudo


def toggle_audio(session: ZoomSession) -> AudioState:
    """Alterna mute/unmute do audio proprio. Retorna novo estado."""
    el = _find_toolbar_button(
        session,
        "btn_muteAudio",
        timeout=2.0,
        fresh=True,
    ) or _get_audio_button(session)
    _safe_invoke(session, el)
    time.sleep(0.15)
    try:
        el = _find_toolbar_button(
            session,
            "btn_muteAudio",
            timeout=1.0,
            fresh=True,
        ) or el
        title = el.window_text() or ""
        return _audio_button_state_from_text(title)
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to read Zoom audio state after toggle")
    return AudioState.UNKNOWN


def mute_audio(session: ZoomSession) -> AudioState:
    """Garante que o audio proprio fica mutado."""
    el = _find_toolbar_button(
        session,
        "btn_muteAudio",
        timeout=2.0,
        fresh=True,
    ) or _get_audio_button(session)
    title = el.window_text() or ""
    state = _audio_button_state_from_text(title)
    if state == AudioState.UNMUTED:
        _safe_invoke(session, el)
        time.sleep(0.15)
        return AudioState.MUTED
    if state == AudioState.DISCONNECTED:
        return AudioState.DISCONNECTED
    return AudioState.MUTED


def unmute_audio(session: ZoomSession) -> AudioState:
    """Garante que o audio proprio fica ativo."""
    el = _find_toolbar_button(
        session,
        "btn_muteAudio",
        timeout=2.0,
        fresh=True,
    ) or _get_audio_button(session)
    title = el.window_text() or ""
    state = _audio_button_state_from_text(title)
    if state == AudioState.MUTED:
        _safe_invoke(session, el)
        time.sleep(0.15)
        return AudioState.UNMUTED
    if state == AudioState.DISCONNECTED:
        return AudioState.DISCONNECTED
    return AudioState.UNMUTED


# ─────────────────────────────────────────────────────────────────
#  Video: start / stop / toggle
# ─────────────────────────────────────────────────────────────────

def _get_video_button(session: ZoomSession):
    """Retorna o elemento btn_muteVideo (ou equivalente na floating toolbar)."""
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    el = _find_toolbar_button(session, "btn_muteVideo", timeout=2.0)
    if not el and ctx.is_floating:
        el = _find_element_by_text(
            win, VIDEO_STOPPED_TEXT + VIDEO_STARTED_TEXT, timeout=2.0
        )
    if not el:
        raise RuntimeError("btn_muteVideo not found")
    return el


def _video_is_stopped(session: ZoomSession) -> bool:
    el = _find_toolbar_button(
        session,
        "btn_muteVideo",
        timeout=2.0,
        fresh=True,
    ) or _get_video_button(session)
    title = el.window_text() or ""
    state = _video_button_state_from_text(title)
    if state == VideoState.STARTED:
        return False
    if state == VideoState.STOPPED:
        return True
    return True  # fallback conservador: assume parado


def toggle_video(session: ZoomSession) -> VideoState:
    """Alterna video ligado/desligado. Retorna novo estado."""
    el = _find_toolbar_button(
        session,
        "btn_muteVideo",
        timeout=2.0,
        fresh=True,
    ) or _get_video_button(session)
    _safe_invoke(session, el)
    time.sleep(0.15)
    try:
        el = _find_toolbar_button(
            session,
            "btn_muteVideo",
            timeout=1.0,
            fresh=True,
        ) or el
        title = el.window_text() or ""
        return _video_button_state_from_text(title)
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to read Zoom video state after toggle")
    return VideoState.UNKNOWN


def start_video(session: ZoomSession) -> VideoState:
    """Garante que o video esta ligado."""
    el = _find_toolbar_button(
        session,
        "btn_muteVideo",
        timeout=2.0,
        fresh=True,
    ) or _get_video_button(session)
    title = el.window_text() or ""
    if _video_button_state_from_text(title) == VideoState.STOPPED:
        _safe_invoke(session, el)
        time.sleep(0.15)
    return VideoState.STARTED


def stop_video(session: ZoomSession) -> VideoState:
    """Garante que o video esta desligado."""
    el = _find_toolbar_button(
        session,
        "btn_muteVideo",
        timeout=2.0,
        fresh=True,
    ) or _get_video_button(session)
    title = el.window_text() or ""
    if _video_button_state_from_text(title) == VideoState.STARTED:
        _safe_invoke(session, el)
        time.sleep(0.15)
    return VideoState.STOPPED


# ─────────────────────────────────────────────────────────────────
#  Painel de participantes
# ─────────────────────────────────────────────────────────────────

def _open_participants_panel(session: ZoomSession) -> bool:
    """
    Abre o painel de participantes se ainda nao estiver aberto.
    Retorna True se foi necessario abrir, False se ja estava aberto.

    Usa detecção robusta (estrutural + bounds) para evitar toggle acidental
    que fecha o painel quando ele já está aberto.

    Double-check: se a primeira verificação diz "fechado", espera 50ms e
    verifica de novo para descartar falsos negativos por glitch de UIA/COM.
    """
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    # Detecção robusta: scan estrutural fresco com bounds check
    if _participants_panel_visible(win):
        return False  # ja estava aberto

    # Double-check: evita toggle acidental por falso negativo transiente.
    # UIA/COM pode falhar em scan isolado (glitch, timing, animação).
    time.sleep(0.05)
    ctx.refresh(force=True)
    win = ctx.get_main_window()
    if _participants_panel_visible(win):
        return False  # aberto — evitou toggle acidental

    if not _invoke_toolbar_button(session, "btn_paticipants", timeout=2.0):
        raise RuntimeError("btn_paticipants not found")

    # Aguarda painel aparecer — usa detecção robusta
    def _panel_appeared():
        ctx.refresh(force=True)
        try:
            w = ctx.get_main_window()
            return _participants_panel_visible(w)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            return None

    if not _poll(_panel_appeared, timeout=POPUP_TIMEOUT):
        raise RuntimeError("Participants panel did not open within timeout")

    # Cache proativo dos botões do painel de participantes (mute_all, more_btn)
    try:
        w = ctx.get_main_window()
        h = ctx.main_handle
        if h:
            _find_elements_by_cid(
                session,
                w,
                PARTICIPANTS_PANEL_CONTROL_IDS,
                use_cache=True, cache_handle=h,
            )
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to warm Zoom participants panel cache")

    return True


def _close_participants_panel(session: ZoomSession) -> None:
    """Fecha o painel de participantes se estiver aberto."""
    ctx = session
    try:
        win = ctx.get_main_window()
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return
    if not _participants_panel_visible(win):
        return
    if _invoke_toolbar_button(session, "btn_paticipants", timeout=1.0):
        time.sleep(0.15)


# ─────────────────────────────────────────────────────────────────
#  Mute all / Unmute all
# ─────────────────────────────────────────────────────────────────

def mute_all(session: ZoomSession, allow_unmute: bool = False) -> None:
    """
    Muta todos os participantes.

    allow_unmute=False  → garante que a checkbox 'Permitir participantes ativarem
                          o proprio audio' fica DESMARCADA (padrao mais restritivo).
    allow_unmute=True   → deixa a checkbox MARCADA.
    """
    ctx = _require_meeting(session)
    opened_by_us = _open_participants_panel(session)
    time.sleep(0.1)

    win      = ctx.get_main_window()
    mute_btn = _find_element_by_cid(
        session,
        win,
        "mute_all_btn",
        timeout=3.0,
    )
    if not mute_btn:
        if opened_by_us:
            _close_participants_panel(session)
        raise RuntimeError("mute_all_btn not found in participants panel")

    _safe_invoke(session, mute_btn)

    # Aguarda caixa de dialogo de confirmacao via poll
    _CONFIRM_CLASSES = (
        "zChangeNameWndClass",
        "ZGridMultiLevelPopupWndClass",
        "WCN_ModelessWnd",
    )

    def _find_confirm_dialog():
        windows, _ = _find_zoom_windows_fast(session)
        for w in windows:
            if w["class_name"] in _CONFIRM_CLASSES:
                try:
                    tw = _get_window(session, w["handle"])
                    chk = _find_element_by_cid(session, tw, "chk_option")
                    if chk:
                        return tw
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        return None

    confirm_win = _poll(_find_confirm_dialog, timeout=POPUP_TIMEOUT)

    if not confirm_win:
        # Sem dialogo = nao havia participantes ou Zoom mutou silenciosamente
        if opened_by_us:
            _close_participants_panel(session)
        return

    # ── Ajusta checkbox ──────────────────────────────────────────
    # chk_option = "Permitir que participantes ativem o proprio audio"
    # allow_unmute=True  → queremos MARCADA   (toggle_state == 1)
    # allow_unmute=False → queremos DESMARCADA (toggle_state == 0)
    chk = _find_element_by_cid(session, confirm_win, "chk_option")
    if chk:
        try:
            toggle_state = chk.get_toggle_state()  # 0=desmarcada, 1=marcada
            needs_toggle = (
                (allow_unmute     and toggle_state == 0) or
                (not allow_unmute and toggle_state == 1)
            )
            if needs_toggle:
                _safe_invoke(session, chk)
                time.sleep(0.1)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Could not read Zoom mute-all checkbox state; skipped blind toggle")

    # ── Confirma o dialogo ───────────────────────────────────────
    # btn_rename e reutilizado pelo Zoom como botao "Sim"/"Yes" nesse contexto
    confirm_btn = _find_element_by_cid(session, confirm_win, "btn_rename")
    if confirm_btn:
        _safe_invoke(session, confirm_btn)
        time.sleep(0.15)
    else:
        try:
            session.perform_action(lambda: send_keys("{ENTER}"))
            time.sleep(0.15)
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to confirm Zoom mute-all dialog with Enter fallback")

    if opened_by_us:
        time.sleep(0.1)
        _close_participants_panel(session)


def unmute_all(session: ZoomSession) -> None:
    """
    Pede que todos os participantes ativem o proprio som.

    Estrategia:
      1. mute_all(allow_unmute=True) — garante que o checkbox
         'Permitir participantes ativarem o proprio audio' esta marcado.
      2. Menu 3 pontinhos (more_btn) → 'Pedir a todos para ativar o som'.
    """
    # Abre o painel uma vez — mute_all vera que ja esta aberto e nao vai fechar.
    opened_by_us = _open_participants_panel(session)
    time.sleep(0.1)

    # Passo 1: garante permissao de desmutar (checkbox marcado)
    mute_all(session, allow_unmute=True)
    time.sleep(0.15)

    # Passo 2: 3 pontinhos → "Pedir a todos para ativar o som"
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    more_btn = _find_element_by_cid(session, win, "more_btn", timeout=2.0)
    if not more_btn:
        if opened_by_us:
            _close_participants_panel(session)
        raise RuntimeError("more_btn (three-dot menu) not found in participants panel")

    _safe_invoke(session, more_btn)

    popup_info = _wait_for_window_class(
        session,
        "WCN_ModelessWnd",
        timeout=POPUP_TIMEOUT,
    )
    if not popup_info:
        if opened_by_us:
            _close_participants_panel(session)
        raise RuntimeError("3-dot menu did not appear")

    popup_win = _get_window(session, popup_info["handle"])

    first_item = None
    for d in popup_win.descendants():
        try:
            if d.friendly_class_name() in ("MenuItem", "Button", "ListItem"):
                # Garante que ignora separadores visuais vazios
                if (d.window_text() or "").strip():
                    first_item = d
                    break
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            continue

    if first_item:
        _safe_invoke(
            session,
            first_item,
            fallback_hwnd=popup_info["handle"],
        )
        time.sleep(0.1)
    else:
        try:
            session.perform_action(lambda: popup_win.type_keys("{ESC}"))
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to close Zoom participants popup after missing menu item")
        if opened_by_us:
            _close_participants_panel(session)
        raise RuntimeError("No interactive item (MenuItem/Button) found in 3-dot popup")

    if opened_by_us:
        time.sleep(0.1)
        _close_participants_panel(session)


# ─────────────────────────────────────────────────────────────────
#  Audio do computador: leave / join
# ─────────────────────────────────────────────────────────────────

def _open_audio_dropdown(session: ZoomSession):
    """
    Abre o dropdown de audio (botao seta ao lado do microfone).
    Retorna o wrapper WCN_ModelessWnd do popup, ou None se nao aparecer.
    """
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    audio_menu = _find_toolbar_button(session, "btn_audioMenu", timeout=2.0)
    if not audio_menu:
        return None

    # Focus necessario para o dropdown seta responder ao click
    try:
        session.perform_action(win.set_focus)
        time.sleep(0.05)
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to focus Zoom window before opening audio dropdown")

    if not _safe_invoke(session, audio_menu):
        return None

    popup_info = _wait_for_window_class(
        session,
        "WCN_ModelessWnd",
        timeout=POPUP_TIMEOUT,
    )
    if not popup_info:
        return None
    return _get_window(session, popup_info["handle"])


def leave_computer_audio(session: ZoomSession) -> None:
    """Sai do audio do computador via menu dropdown."""
    popup_win = _open_audio_dropdown(session)
    if not popup_win:
        raise RuntimeError("btn_audioMenu not found or dropdown did not appear")

    if _click_in_popup(session, popup_win, _LEAVE_AUDIO_PATTERNS):
        return

    if _click_penultimate_audio_menu_item(session, popup_win):
        return

    try:
        session.perform_action(lambda: popup_win.type_keys("{ESC}"))
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to close Zoom audio dropdown after missing leave item")
    raise RuntimeError("'Leave Computer Audio' item not found in audio menu")


def _click_penultimate_audio_menu_item(
    session: ZoomSession,
    popup_win,
) -> bool:
    """Zoom keeps 'Leave audio' as the penultimate visible item in this menu."""
    items = []
    try:
        for d in popup_win.descendants():
            try:
                if d.friendly_class_name() not in {"MenuItem", "Button"}:
                    continue
                if not _rect_is_visible(d):
                    continue
                title = (d.window_text() or "").strip()
                if not title:
                    continue
                rect = d.rectangle()
                if rect.height() < 18:
                    continue
                if re.fullmatch(r"\d+\s+of\s+\d+", title, flags=re.IGNORECASE):
                    continue
                items.append(d)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        return False
    items.sort(key=lambda item: (item.rectangle().top, item.rectangle().left))
    if len(items) < 2:
        return False
    _safe_invoke(session, items[-2])
    time.sleep(0.1)
    return True


def _find_join_audio_btn_in_popup(popup_win):
    """
    Busca o botao de entrar no audio dentro do popup zJoinAudioWndClass.
    Usa multiplas estrategias em uma unica passagem de descendants.
    """
    best_by_cid = None
    best_by_text = None
    best_by_position = None
    try:
        for d in popup_win.descendants():
            try:
                if not best_by_position and d.friendly_class_name() == "Button" and _rect_is_visible(d):
                    best_by_position = d
                cid = _extract_control_id(d)
                if cid:
                    if cid in ("btn_connectAudio", "btn_joinAudio"):
                        return d
                    if "audio" in cid.lower() and d.friendly_class_name() == "Button":
                        best_by_cid = d
                if not best_by_text:
                    title = d.window_text() or ""
                    if title and _matches_any(title, _JOIN_AUDIO_BTN_PATTERNS):
                        best_by_text = d
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
    except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
        _debug_ignored("Failed to scan Zoom join-audio popup")
    return best_by_cid or best_by_text or best_by_position


def join_computer_audio(session: ZoomSession) -> None:
    """Entra no audio do computador."""
    ctx = _require_meeting(session)
    win = ctx.get_main_window()

    # ── Caso 1: btn_muteAudio exibe "Conectar áudio" (audio desconectado) ──
    mute_btn = _find_toolbar_button(
        session,
        "btn_muteAudio",
        timeout=1.0,
        fresh=True,
    )
    if mute_btn:
        title = mute_btn.window_text() or ""
        if _audio_button_state_from_text(title) == AudioState.DISCONNECTED:
            _safe_invoke(session, mute_btn)

            join_popup = _wait_for_window_class(
                session,
                "zJoinAudioWndClass",
                timeout=POPUP_TIMEOUT,
            )
            if join_popup:
                popup_win = _get_window(session, join_popup["handle"])
                join_btn = _find_join_audio_btn_in_popup(popup_win)
                if not join_btn:
                    # Poll: popup pode demorar a popular seus filhos
                    def _find():
                        return _find_join_audio_btn_in_popup(
                            _get_window(session, join_popup["handle"])
                        )
                    join_btn = _poll(_find, timeout=3.0)
                if join_btn:
                    _safe_invoke(session, join_btn)
                    time.sleep(0.3)
                    return
                try:
                    session.perform_action(lambda: popup_win.type_keys("{ESC}"))
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    _debug_ignored("Failed to close Zoom join-audio popup after missing button")
                raise RuntimeError("Join audio button not found in popup (tried controlID + text)")
            return  # reconectou direto sem popup

    # ── Caso 2: btn_connectAudio explicito (variante do Zoom) ──
    connect_btn = _find_element_by_cid(
        session,
        win,
        "btn_connectAudio",
        timeout=1.0,
    )
    if connect_btn:
        _safe_invoke(session, connect_btn)
        join_popup = _wait_for_window_class(
            session,
            "zJoinAudioWndClass",
            timeout=POPUP_TIMEOUT,
        )
        if join_popup:
            popup_win = _get_window(session, join_popup["handle"])
            join_btn = _find_join_audio_btn_in_popup(popup_win)
            if not join_btn:
                def _find():
                    return _find_join_audio_btn_in_popup(
                        _get_window(session, join_popup["handle"])
                    )
                join_btn = _poll(_find, timeout=3.0)
            if join_btn:
                _safe_invoke(session, join_btn)
                time.sleep(0.3)
                return
            try:
                session.perform_action(lambda: popup_win.type_keys("{ESC}"))
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                _debug_ignored("Failed to close Zoom join-audio popup")
        return  # reconectou direto

    raise RuntimeError("Could not find any join audio button (btn_muteAudio or btn_connectAudio)")


def _force_foreground(hwnd: int) -> bool:
    """Forca uma janela para foreground via AttachThreadInput. Sem mouse."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    cur_fg = user32.GetForegroundWindow()
    if cur_fg == hwnd:
        return True

    fg_thread = user32.GetWindowThreadProcessId(cur_fg, None)
    my_thread = kernel32.GetCurrentThreadId()

    user32.AttachThreadInput(my_thread, fg_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(my_thread, fg_thread, False)

    time.sleep(0.1)
    return user32.GetForegroundWindow() == hwnd


def stop_screen_share(session: ZoomSession) -> None:
    """
    Interrompe o compartilhamento de tela ativo de forma ultrarrápida.
    """
    def _float_gone() -> bool:
        # Considera que o compartilhamento acabou se a janela sumir OU ficar invisível
        return not bool(_find_float_toolbar_hwnd())

    stop_cache = session.stop_share_cache
    cached_btn = stop_cache.get()

    if cached_btn:
        try:
            _safe_invoke(
                session,
                cached_btn,
                fallback_hwnd=stop_cache.float_handle,
            )
            stop_cache.invalidate()
            if _poll(_float_gone, timeout=1.0):
                return
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            stop_cache.invalidate()

    time.sleep(4)  # Breve pausa para UI reagir
    float_handle = _find_float_toolbar_hwnd()

    if float_handle:
        btn = _scan_stop_btn(session, float_handle)
        
        if btn:
            try:
                _safe_invoke(session, btn, fallback_hwnd=float_handle)
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                _debug_ignored("Failed to invoke Zoom stop-share button")
            
            if _poll(_float_gone, timeout=1.0):
                return

    raise RuntimeError(
        "stop_screen_share failed -- floating toolbar not found or button did not respond"
    )




# ─────────────────────────────────────────────────────────────────
#  Debug
# ─────────────────────────────────────────────────────────────────

def dump_descendants(win, max_items: int = 200) -> None:
    """Loga todos os descendentes de uma janela (util para debug/mapeamento)."""
    count = 0
    for d in win.descendants():
        try:
            cid   = _extract_control_id(d)
            title = d.window_text() or ""
            ctype = d.friendly_class_name()
            cname = d.class_name()
            cid_s = f"  cid={cid}" if cid else ""
            log.debug(
                '  [%s] %-20s class=%-30s "%s"%s',
                d.handle,
                ctype,
                cname,
                title[:60],
                cid_s,
            )
            count += 1
            if count >= max_items:
                log.debug("  ... (truncated at %s items)", max_items)
                break
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            continue


# ─────────────────────────────────────────────────────────────────
#  Participantes: nomes e contagem de pessoas
# ─────────────────────────────────────────────────────────────────

def get_participant_names(session: ZoomSession) -> list[str]:
    """
    Retorna a lista de nomes dos participantes da reuniao.
    Abre o painel de participantes se necessario e fecha ao terminar.
    Extrai o texto do elemento com controlID='username_lb' de cada ListItem.
    """
    ctx = _require_meeting(session)
    opened_by_us = _open_participants_panel(session)
    try:
        time.sleep(0.1)
        win = ctx.get_main_window()
        # Tenta cache/fallback estrutural primeiro (rápido), depois controlID
        list_el = _find_participants_fallback(win, "unified_primary_list")
        if not list_el:
            list_el = _find_element_by_cid(
                session,
                win,
                "unified_primary_list",
                timeout=2.0,
            )
        if not list_el:
            return []
        names: list[str] = []
        found_by_id = False
        for d in list_el.descendants():
            try:
                cid = _extract_control_id(d)
                if cid == "username_lb":
                    name = d.window_text() or ""
                    if name.strip():
                        names.append(name.strip())
                        found_by_id = True
            except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                continue
        if found_by_id:
            return names

        # Zoom 7.0.2 removed username_lb. Each participant is a ListItem; the
        # first visible Static child is the display name, followed by role/status.
        try:
            for item in list_el.descendants():
                try:
                    if item.friendly_class_name() != "ListItem":
                        continue
                    statics = []
                    for child in item.descendants():
                        try:
                            if child.friendly_class_name() == "Static":
                                text = (child.window_text() or "").strip()
                                if text and not text.startswith("("):
                                    statics.append(text)
                        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                            continue
                    if statics:
                        names.append(statics[0])
                    else:
                        text = (item.window_text() or "").strip()
                        if text:
                            names.append(text.split(",")[0].strip())
                except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
                    continue
        except Exception:  # noqa: BLE001 - pywinauto/UIA adapter boundary
            _debug_ignored("Failed to collect Zoom participant names")
        return names
    finally:
        if opened_by_us:
            _close_participants_panel(session)

def _explicit_people_count(name: str) -> int | None:
    match = re.search(r"\s+([1-9]\d{0,2})\s*$", name.strip())
    if not match:
        return None
    value = int(match.group(1))
    if value > 99:
        return None
    return value


def count_people(names: list[str], exclude_host: bool = True) -> int:
    """
    Conta o numero real de pessoas a partir dos nomes de participantes.

    Um numero inteiro no fim do nome vence os separadores.
    Ex.: "Familia Alves 7" conta como 7.

    Usa PEOPLE_SEPARATORS para detectar multiplas pessoas em um nome.
    O separador recomendado e ' & ' (ex: 'Felipe & Julia').
    Tambem aceita ' | ', ' + '.

    Se exclude_host=True, subtrai 1 para descontar o anfitriao.
    """
    total = 0
    for name in names:
        explicit_count = _explicit_people_count(name)
        if explicit_count is not None:
            total += explicit_count
            continue
        people_in_entry = 1
        for sep in PEOPLE_SEPARATORS:
            if sep in name:
                people_in_entry = name.count(sep) + 1
                break
        total += people_in_entry
    if exclude_host and total > 0:
        total -= 1
    return total
