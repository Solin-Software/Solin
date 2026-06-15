"""
timer_output_controller.py — Solin
==================================
Owns the advanced-timer clock windows: it places a :class:`TimerOutputWindow`
on every monitor reserved for the timer (and currently set to *visible*), and
keeps that set in sync as monitors are hot-plugged or the reservation/visibility
changes.

This is the timer-side analogue of
:class:`solin.controllers.projection_window_controller.ProjectionWindowController`
and consults the same shared :class:`MonitorAllocationStore`.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from ..core.timer.application import TimerSession
from ..core.timer.models import ClockConfig
from ..core.ui.monitor_allocation import OWNER_TIMER
from ..core.ui.screens import ScreenManager
from ..projection.timer_window import TimerOutputWindow
from ..ui.qml.timer_output import ClockRenderBridge


class TimerOutputController:
    """Manages the fullscreen clock windows across reserved monitors."""

    def __init__(
        self,
        engine,
        session: TimerSession,
        allocation,
    ) -> None:
        self._engine = engine
        self._session = session
        self._allocation = allocation

        self._clock_config = session.clock_config
        self._engine.set_direction(self._clock_config.direction)
        self._engine.set_freeze_seconds(self._clock_config.freeze_seconds)

        # One shared render bridge feeds every clock window.
        self._bridge = ClockRenderBridge(engine, lambda: self._clock_config)
        self._windows: list[TimerOutputWindow] = []

    # ── Configuration ──────────────────────────────────────────────────────

    def set_clock_config(self, config: ClockConfig) -> None:
        """Apply a new ClockConfig: persist, sync direction, repaint windows."""
        self._clock_config = config.clamped()
        self._engine.set_direction(self._clock_config.direction)
        self._engine.set_freeze_seconds(self._clock_config.freeze_seconds)
        self._bridge.refresh_now()

    @property
    def clock_config(self):
        return self._clock_config

    # ── Visibility (reserve vs. show) ──────────────────────────────────────

    def set_visible(self, visible: bool) -> None:
        self._session.set_timer_visible(bool(visible))
        self.reconcile()

    def is_visible(self) -> bool:
        return self._session.timer_visible()

    # ── Screen-change wiring ───────────────────────────────────────────────

    def on_screens_changed(self) -> None:
        # Same 700 ms settle delay the media side uses for DWM/virtual-desktop.
        QTimer.singleShot(700, self.reconcile)

    # ── Reconciliation ─────────────────────────────────────────────────────

    def _desired_screens(self) -> list:
        if not self._session.timer_visible():
            return []
        return [
            s for s in ScreenManager.secondary_screens()
            if self._allocation.owner_of(s) == OWNER_TIMER
        ]

    def reconcile(self) -> None:
        """Create/destroy clock windows so they match the reserved+visible set."""
        desired = self._desired_screens()
        desired_names = {s.name() for s in desired}

        # Drop windows whose screen is no longer reserved/visible; refit the rest.
        keep: list[TimerOutputWindow] = []
        connected = {s.name(): s for s in ScreenManager.secondary_screens()}
        for win in self._windows:
            name = win.screen().name() if win.screen() else None
            if name in desired_names and name in connected:
                win.refit_to_screen(connected[name])
                keep.append(win)
            else:
                win.fade_out_and_close()
        self._windows = keep

        # Spawn windows for newly desired screens.
        existing = {win.screen().name() for win in self._windows if win.screen()}
        for i, screen in enumerate(desired, 1):
            if screen.name() in existing:
                continue
            self._windows.append(
                TimerOutputWindow(screen, self._bridge, monitor_index=i)
            )

    def has_windows(self) -> bool:
        return bool(self._windows)

    def close_all(self) -> None:
        # Immediate close (no fade) — used on app shutdown where the event loop
        # is tearing down and an animation would never finish.
        for win in self._windows:
            win.close()
        self._windows.clear()
