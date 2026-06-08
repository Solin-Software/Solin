from __future__ import annotations

import os

from PySide6.QtCore import QDateTime, QTimer

from ..core.ui.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER
from ..core.ui.screens import ScreenManager
from ..projection import FloatingPreviewWindow, ProjectionWindow
from ..projection.idle_source import IdleMediaSource


class ProjectionWindowController:
    """Owns projection target windows and monitor-manager actions."""

    def __init__(self, window, *, idle_source_factory=IdleMediaSource) -> None:
        self._window = window
        # The single shared idle decoder is created lazily on first use so the
        # controller stays cheap to construct (and unit-testable without a
        # QApplication).  All surfaces paint the frames it fans out, so the idle
        # video is decoded exactly once and stays frame-locked across monitors.
        self._idle_source_factory = idle_source_factory
        self._idle_source: IdleMediaSource | None = None

    # ── Monitor allocation helpers ─────────────────────────────────────────
    #
    # The shared MonitorAllocationStore (when present on the window) is the
    # persistent source of truth for which subsystem owns each screen. All the
    # logic degrades gracefully to the legacy in-memory ``_deactivated_screens``
    # set when no store is wired (e.g. in unit tests with a window stub).

    def _allocation(self):
        return getattr(self._window, "_monitor_allocation", None)

    def _media_eligible(self, screen) -> bool:
        """True when media projection windows may be shown on ``screen``."""
        alloc = self._allocation()
        if alloc is None:
            return screen.name() not in self._window._deactivated_screens
        return alloc.owner_of(screen) == OWNER_MEDIA

    def _is_timer_reserved(self, screen) -> bool:
        alloc = self._allocation()
        return alloc is not None and alloc.owner_of(screen) == OWNER_TIMER

    def _persist_media_owner(self, screen, active: bool) -> None:
        """Record an explicit media show/hide choice in the shared store."""
        alloc = self._allocation()
        if alloc is None:
            return
        # Never clobber a timer reservation from the media side here.
        if alloc.owner_of(screen) == OWNER_TIMER:
            return
        alloc.set_owner(screen, OWNER_MEDIA if active else OWNER_OFF)

    def open_projection_windows(self) -> None:
        window = self._window
        for w in window.projection_windows:
            w.close()
        window.projection_windows.clear()

        self._normalize_expired_state()

        secondary = ScreenManager.secondary_screens()
        idx = 0
        for screen in secondary:
            if not self._media_eligible(screen):
                continue
            idx += 1
            win = ProjectionWindow(screen, idx)
            window.projection_windows.append(win)

        for win in window.projection_windows:
            self.apply_full_state_to_window(win)

        self._set_screen_count(len(window.projection_windows))
        window._projection_integrations.sync_projection_integrations()

    def apply_yearly_text(self, quote: str, ref: str, api_code: str = "") -> None:
        for win in self.all_windows():
            win.set_yearly_text(quote, ref, api_code)

    def apply_full_state_to_window(self, win) -> None:
        """Single source of truth for what a projection surface must show.

        Applies the *complete* current projection state to one window — yearly
        text, custom idle media, and the active projection (video/image/timer/
        sermon).  Every window-creation path funnels through here so a freshly
        created or hot-plugged monitor always matches the windows that were
        already open, instead of each call site re-deriving a partial subset of
        the state (which is exactly how the idle-media-on-reconnect bug crept
        in: reconcile replayed everything *except* the idle media).
        """
        # Track this surface's idle visibility so the shared decoder can pause
        # while no surface is showing the idle video (Qt auto-disconnects when
        # the window is destroyed, so there is nothing to clean up on close).
        win.bind_idle_visibility(self._on_idle_visibility_changed)

        quote, ref, api_code = self._yearly_text()
        win.set_yearly_text(quote, ref, api_code)
        if self._window._idle_media_path:
            win.set_idle_active()
            # Paint the most recent decoded frame right away so a hot-plugged or
            # respawned surface is in sync from its very first frame instead of
            # flashing black until the next frame arrives.
            if self._idle_source is not None and self._idle_source.current_image is not None:
                win.update_idle_image(self._idle_source.current_image)
        else:
            win.clear_idle()
        self.restore_state_to_window(win)

    def _ensure_idle_source(self) -> IdleMediaSource:
        """Create (once) and return the shared idle decoder, wiring its frames to
        every surface."""
        if self._idle_source is None:
            self._idle_source = self._idle_source_factory()
            self._idle_source.frame_ready.connect(self._distribute_idle_frame)
        return self._idle_source

    def _distribute_idle_frame(self, image) -> None:
        """Fan a freshly decoded idle frame out to every surface (one decode, N
        paints — mirrors the normal-video distribute_frame pipeline)."""
        for win in self.all_windows():
            win.update_idle_image(image)

    def _on_idle_visibility_changed(self, _visible: bool) -> None:
        """A surface showed/hid its idle page — re-evaluate decoder playback."""
        self._sync_idle_playback()

    def _sync_idle_playback(self) -> None:
        """Play the shared idle video only while at least one surface is showing
        the idle screen; pause it otherwise (no-op for image idles)."""
        if self._idle_source is None:
            return
        any_visible = any(win.is_idle_visible() for win in self.all_windows())
        self._idle_source.set_playing(any_visible)

    def _normalize_expired_state(self) -> None:
        """Collapse a timer whose target has already passed back to idle.

        Kept separate from apply_full_state_to_window because it mutates the
        *shared* state once, not per-window.  Without it an expired countdown
        would be replayed (and silently ignored) on every new window forever.
        """
        state = getattr(self._window, "_proj_state", {"type": "idle"})
        if state.get("type") != "timer":
            return
        now = QDateTime.currentDateTime()
        if now.secsTo(state["target_dt"]) <= 0:
            self._window._proj_state = {"type": "idle"}
            self._window._projection_integrations.sync_obs_scene(False)

    def on_screens_changed(self) -> None:
        # 700 ms gives Windows DWM enough time to finish reorganising the
        # virtual desktop and settle all QScreen geometries before reading them.
        QTimer.singleShot(700, self.reconcile_projection_windows)

    @staticmethod
    def _screen_name(screen) -> str | None:
        """Safely read a QScreen's name.

        A monitor can be unplugged at any moment, after which Qt deletes the
        underlying C++ ``QScreen`` while a Python reference still lingers (e.g.
        captured in a projection window). Touching it then raises ``RuntimeError``.
        Returning ``None`` lets callers treat such screens as gone.
        """
        try:
            return screen.name()
        except RuntimeError:
            return None

    def reconcile_projection_windows(self) -> None:
        window = self._window
        # Filter out any screens Qt has already deleted under us.
        secondary = [
            s for s in ScreenManager.secondary_screens()
            if self._screen_name(s) is not None
        ]
        connected: dict[str, object] = {self._screen_name(s): s for s in secondary}

        still_valid: list[ProjectionWindow] = []
        for win in window.projection_windows:
            win_name = self._screen_name(win.screen())
            fresh_screen = connected.get(win_name) if win_name is not None else None
            # Drop windows on disconnected/deleted screens *and* on screens that
            # are no longer media-eligible (e.g. just reserved for the timer) so a
            # timer reservation hides the media window without a manual step.
            if fresh_screen is None or not self._media_eligible(fresh_screen):
                win.fade_out_and_close()
            else:
                win.refit_to_screen(fresh_screen)
                still_valid.append(win)
        window.projection_windows = still_valid

        existing_names = {
            n for n in (self._screen_name(win.screen()) for win in window.projection_windows)
            if n is not None
        }
        self._normalize_expired_state()

        for i, screen in enumerate(secondary):
            name = self._screen_name(screen)
            if name is None or name in existing_names:
                continue
            if not self._media_eligible(screen):
                continue
            win = ProjectionWindow(screen, i + 1)
            self.apply_full_state_to_window(win)
            window.projection_windows.append(win)

        self._set_screen_count(len(window.projection_windows))
        window._projection_integrations.sync_projection_integrations()

    def all_windows(self) -> list:
        wins = list(self._window.projection_windows)
        if self._window.floating_preview_window is not None:
            wins.append(self._window.floating_preview_window)
        return wins

    def on_idle_media_changed(self, path: str) -> None:
        window = self._window
        # Reject a path that no longer exists — treat it as "clear".
        if path and not os.path.isfile(path):
            path = ""
        window._idle_media_path = path

        if path:
            # Activate surfaces *before* loading: a static image emits its single
            # frame synchronously inside set_media(), so the surfaces must already
            # be in idle-media mode to accept it.  Decode happens once on the
            # shared source; every surface paints the frames it fans out (see
            # _distribute_idle_frame).
            for win in self.all_windows():
                win.set_idle_active()
            self._ensure_idle_source().set_media(path)
            # Start the video decoder only if the idle screen is actually visible
            # right now (it is not while a clip/image/timer is being projected).
            self._sync_idle_playback()
        else:
            if self._idle_source is not None:
                self._idle_source.clear()
            for win in self.all_windows():
                win.clear_idle()

        if window._monitor_popup is not None:
            window._monitor_popup._sync_idle_ui(path)

    def on_floating_toggle(self, make_active: bool) -> None:
        window = self._window
        if make_active:
            if window.floating_preview_window is None:
                window.floating_preview_window = self.create_floating_window()
        else:
            if window.floating_preview_window is not None:
                window.floating_preview_window.close()
                window.floating_preview_window = None

        window._projection_integrations.sync_projection_integrations()
        QTimer.singleShot(
            420,
            lambda: self.on_monitor_manager_requested(window._quick_toolbar._monitor_btn),
        )

    def create_floating_window(self) -> FloatingPreviewWindow:
        quote, ref, api_code = self._yearly_text()
        win = FloatingPreviewWindow(
            yearly_text_quote=quote,
            yearly_text_ref=ref,
            api_code=api_code,
        )
        win.respawn_requested.connect(self.on_floating_respawn)
        self.apply_full_state_to_window(win)
        return win

    def on_floating_respawn(self, saved_geo) -> None:
        self._window.floating_preview_window = None
        QTimer.singleShot(120, lambda: self.respawn_floating_silent(saved_geo))

    def respawn_floating_silent(self, saved_geo) -> None:
        window = self._window
        if window.floating_preview_window is not None:
            return
        window.floating_preview_window = self.create_floating_window()
        QTimer.singleShot(0, lambda: (
            window.floating_preview_window.setGeometry(saved_geo)
            if window.floating_preview_window is not None else None
        ))

    def on_monitor_manager_requested(self, anchor_widget) -> None:
        window = self._window
        popup = window._monitor_popup
        secondary = ScreenManager.secondary_screens()
        active_screens = {id(win.screen()): win for win in window.projection_windows}

        alloc = self._allocation()
        screens_info = []
        for i, screen in enumerate(secondary):
            screens_info.append({
                "screen": screen,
                "active": id(screen) in active_screens,
                "index": i,
                "timer_reserved": alloc is not None and alloc.owner_of(screen) == OWNER_TIMER,
            })

        floating_active = window.floating_preview_window is not None
        popup.populate(
            screens_info,
            floating_active=floating_active,
            idle_media_path=window._idle_media_path,
        )
        popup.show_above(anchor_widget)

    def on_monitor_toggle(self, screen_index: int, make_active: bool) -> None:
        window = self._window
        secondary = ScreenManager.secondary_screens()
        if screen_index >= len(secondary):
            return
        screen = secondary[screen_index]
        screen_name = screen.name()

        if make_active:
            if self._is_timer_reserved(screen):
                # Taking a screen the timer reserved needs explicit confirmation.
                if not self._confirm_take_timer_screen(screen):
                    return
                # Do every screen-referencing operation *before* releasing the
                # clock: tearing the clock window down can make the OS
                # re-enumerate and invalidate the captured `screen` object.
                window._deactivated_screens.discard(screen_name)
                self._allocation().confirm_assignment(screen, OWNER_MEDIA)
                self._release_timer_on(screen)
                self._notify_timer_monitors_changed()
                # Rebuild media windows through reconcile, which re-queries the
                # *current* screen list — the same authoritative path used for
                # hot-plug and "unreserve", and the reason this works where a
                # manual create from the now-stale `screen` did not.
                self.reconcile_projection_windows()
            else:
                window._deactivated_screens.discard(screen_name)
                self._persist_media_owner(screen, active=True)
                already = any(win.screen() == screen for win in window.projection_windows)
                if not already:
                    win = ProjectionWindow(screen, screen_index + 1)
                    window.projection_windows.append(win)
                    self.apply_full_state_to_window(win)
                    self._set_screen_count(len(window.projection_windows))
        else:
            window._deactivated_screens.add(screen_name)
            self._persist_media_owner(screen, active=False)
            to_remove = [win for win in window.projection_windows if win.screen() == screen]
            for win in to_remove:
                window.projection_windows.remove(win)
                win.fade_out_and_close()
            self._set_screen_count(len(window.projection_windows))

        window._projection_integrations.sync_projection_integrations()
        QTimer.singleShot(
            420,
            lambda: self.on_monitor_manager_requested(window._quick_toolbar._monitor_btn),
        )

    def on_monitor_all(self, make_active: bool) -> None:
        window = self._window
        if make_active:
            window._deactivated_screens.clear()
            # "Project all" claims every non-timer-reserved screen for media.
            for screen in ScreenManager.secondary_screens():
                if not self._is_timer_reserved(screen):
                    self._persist_media_owner(screen, active=True)
            self.open_projection_windows()
        else:
            for screen in ScreenManager.secondary_screens():
                window._deactivated_screens.add(screen.name())
                self._persist_media_owner(screen, active=False)
            for win in window.projection_windows:
                win.fade_out_and_close()
            window.projection_windows.clear()
            self._set_screen_count(0)
            window._projection_integrations.sync_projection_integrations()
        window._monitor_popup.hide_animated()

    # ── Conflict handling (media side) ─────────────────────────────────────

    def _confirm_take_timer_screen(self, screen) -> bool:
        """Native confirmation before media displaces the timer on a screen."""
        from PySide6.QtWidgets import QMessageBox

        name = screen.name() or self._window.tr("this monitor")
        box = QMessageBox(self._window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self._window.tr("Monitor in use by the timer"))
        box.setText(
            self._window.tr(
                "The timer is currently using {monitor}. Move media here and "
                "hide the timer on this monitor?"
            ).replace("{monitor}", str(name))
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _release_timer_on(self, screen) -> None:
        """Ask the timer-output controller to drop its window from ``screen``."""
        timer_output = getattr(self._window, "_timer_output", None)
        if timer_output is not None and hasattr(timer_output, "reconcile"):
            timer_output.reconcile()

    def _notify_timer_monitors_changed(self) -> None:
        """Refresh the Timer tab's monitor grid after a media-side takeover so
        the reserved monitor flips back to 'Reserve for timer'."""
        timer_widget = getattr(self._window, "timer_widget", None)
        bridge = getattr(timer_widget, "_bridge", None) if timer_widget else None
        if bridge is not None:
            bridge.refreshMonitors()

    def restore_state_to_window(self, win) -> None:
        state = getattr(self._window, "_proj_state", {"type": "idle"})
        kind = state.get("type", "idle")
        if kind == "idle":
            return
        if kind == "video":
            if not state.get("is_audio", False):
                win.begin_video()
        elif kind == "image":
            win.show_image_from_url_data(state["data"])
            self._replay_transform(win, state)
        elif kind == "sermon_theme":
            win.show_sermon_theme(state["text"], state["subtitle"])
            self._replay_transform(win, state)
        elif kind == "timer":
            now = QDateTime.currentDateTime()
            remaining = now.secsTo(state["target_dt"])
            total = state.get("total", max(1, remaining))
            if remaining > 0:
                win.show_timer(remaining, total)

    @staticmethod
    def _replay_transform(win, state) -> None:
        """Re-apply a persisted zoom/pan onto a freshly created surface.

        Snapped (no animation) so the new surface matches the others instantly.
        Shared by the image and sermon-theme branches — both render through a
        zoom/pan-capable widget and store the transform identically.
        """
        transform = state.get("transform")
        if transform and tuple(transform) != (1.0, 0.0, 0.0):
            win.set_image_transform(*transform, animate=False)

    def _yearly_text(self) -> tuple[str, str, str]:
        quote, ref = self._window.settings_widget.get_yearly_text()
        return (
            quote,
            ref,
            self._window.settings_widget._current_api_code(),
        )

    def _set_screen_count(self, count: int) -> None:
        self._window.proj_bar.set_screen_count(count)
        self._window._quick_toolbar.set_screen_count(count)
