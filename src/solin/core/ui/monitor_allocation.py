r"""
monitor_allocation.py — Solin
=============================
A single, profile-persisted arbiter of *which subsystem owns which monitor*.

Both the media projection windows and the advanced-timer clock window are
fullscreen and mutually exclusive on a given screen, so they must not fight over
the same display. This module is the shared source of truth:

    owner ∈ { "media", "timer", "off" }   (default for unseen screens: "media")

    • media — media projection windows show here (the historical default, so a
              freshly connected monitor still auto-shows media).
    • timer — reserved for the advanced timer clock window. Counts as *occupied*
              for media even when the clock is hidden (reserve ≠ show).
    • off   — the operator explicitly removed media here and assigned nothing;
              persisted so the choice survives a restart (fixes the old bug
              where media reappeared on every reconnect).

Screens are keyed by a **stable identity** (manufacturer/model/serial/name) so
the mapping survives reconnects and reboots, not just by the volatile
``QScreen.name()`` (e.g. ``\\.\DISPLAY2``) which the OS may reshuffle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_MONITORS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

# ── Owner constants ───────────────────────────────────────────────────────────

OWNER_MEDIA = "media"
OWNER_TIMER = "timer"
OWNER_OFF = "off"
_DEFAULT_OWNER = OWNER_MEDIA

# ── Stable screen identity ────────────────────────────────────────────────────

class ScreenIdentity:
    """Builds a stable key for a QScreen (or any object exposing the same API)."""

    @staticmethod
    def key(screen) -> str:
        if screen is None:
            return ""
        manufacturer = _safe_call(screen, "manufacturer").strip()
        model = _safe_call(screen, "model").strip()
        serial = _safe_call(screen, "serialNumber").strip()
        name = _safe_call(screen, "name").strip()

        # A serial number uniquely identifies the physical panel — the volatile
        # OS name (e.g. \\.\DISPLAY2, which can be reshuffled on reconnect) is
        # deliberately excluded so the key is stable across reconnects/reboots.
        if serial:
            return "|".join(p for p in (manufacturer, model, serial) if p)
        # No serial: fall back to make/model plus the name to disambiguate two
        # identical models, accepting slightly weaker reconnect robustness.
        if manufacturer or model:
            return "|".join(p for p in (manufacturer, model, name) if p)
        # Nothing stable at all: name + geometry is the last resort.
        return f"{name}|{_safe_geometry(screen)}"


def _safe_call(obj, attr: str) -> str:
    try:
        fn = getattr(obj, attr, None)
        if fn is None:
            return ""
        val = fn() if callable(fn) else fn
        return str(val) if val is not None else ""
    except Exception:  # noqa: BLE001 - defensive QScreen capability probe
        return ""


def _safe_geometry(screen) -> str:
    try:
        g = screen.geometry()
        return f"{g.x()},{g.y()},{g.width()},{g.height()}"
    except Exception:  # noqa: BLE001 - defensive QScreen geometry probe
        return ""


# ── Conflict descriptor ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConflictInfo:
    """Returned when an assignment would displace the other subsystem."""

    screen_key: str
    screen_name: str
    current_owner: str
    requested_owner: str


# ── Allocation store ──────────────────────────────────────────────────────────

class MonitorAllocationStore:
    """Profile-scoped persistent map of screen-key → owner."""

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "MonitorAllocationStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_MONITORS_APP,
            )
        )

    def _load(self) -> dict[str, str]:
        raw = self._settings.string(SettingsKey.MONITOR_ALLOCATION)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _save(self, mapping: dict[str, str]) -> None:
        # Don't persist defaults — keep the map compact.
        compact = {k: v for k, v in mapping.items() if v != _DEFAULT_OWNER}
        self._settings.set_value(SettingsKey.MONITOR_ALLOCATION, json.dumps(compact))

    # ── Queries ───────────────────────────────────────────────────────────────

    def owner_of(self, screen) -> str:
        return self._load().get(ScreenIdentity.key(screen), _DEFAULT_OWNER)

    def is_timer_reserved(self, screen) -> bool:
        return self.owner_of(screen) == OWNER_TIMER

    def is_media_eligible(self, screen) -> bool:
        return self.owner_of(screen) == OWNER_MEDIA

    def timer_screens(self, live_screens) -> list:
        return [s for s in live_screens if self.owner_of(s) == OWNER_TIMER]

    def media_off_names(self, live_screens) -> set[str]:
        """Names of live screens the operator hid media on (owner == off)."""
        return {
            _safe_call(s, "name")
            for s in live_screens
            if self.owner_of(s) == OWNER_OFF
        }

    # ── Mutations ─────────────────────────────────────────────────────────────

    def set_owner(self, screen, owner: str) -> None:
        mapping = self._load()
        key = ScreenIdentity.key(screen)
        if not key:
            return
        if owner == _DEFAULT_OWNER:
            mapping.pop(key, None)
        else:
            mapping[key] = owner
        self._save(mapping)

    def request_assignment(self, screen, new_owner: str) -> ConflictInfo | None:
        """Try to assign ``new_owner``; return a conflict instead of overwriting
        when the screen currently belongs to the *other* fullscreen subsystem.

        ``off`` and the matching owner are never conflicts. media↔timer is.
        """
        current = self.owner_of(screen)
        contended = {OWNER_MEDIA, OWNER_TIMER}
        if (current in contended and new_owner in contended
                and current != new_owner):
            return ConflictInfo(
                screen_key=ScreenIdentity.key(screen),
                screen_name=_safe_call(screen, "name"),
                current_owner=current,
                requested_owner=new_owner,
            )
        self.set_owner(screen, new_owner)
        return None

    def confirm_assignment(self, screen, new_owner: str) -> None:
        """Force the assignment after the user confirmed a conflict."""
        self.set_owner(screen, new_owner)
