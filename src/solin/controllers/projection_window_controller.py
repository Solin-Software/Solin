from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from typing import Any

from PySide6.QtCore import QCoreApplication, QDateTime, QTimer, QT_TRANSLATE_NOOP

from ..ui.screens import ScreenManager
from ..ui.thumbnail_images import load_thumbnail_path
from ..core.projection.application import ProjectionSession
from ..core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    image_transform_from_values,
)
from ..core.projection.idle_media import IdleMediaRequest, existing_idle_media_path
from ..core.timer.models import MediaCountdownPresentation
from ..projection.window import FloatingPreviewWindow, ProjectionWindow
from ..widgets.projection.idle_dialog import confirm_set_as_idle

_TR_CONTEXT = "ProjectionWindowManagement"
_THIS_MONITOR_SOURCE = QT_TRANSLATE_NOOP(
    "ProjectionWindowManagement",
    "this monitor",
)
_MONITOR_IN_USE_TITLE = QT_TRANSLATE_NOOP(
    "ProjectionWindowManagement",
    "Monitor in use by the timer",
)
_TAKE_TIMER_MONITOR_SOURCE = QT_TRANSLATE_NOOP(
    "ProjectionWindowManagement",
    "The timer is currently using {monitor}. Move media here and hide the "
    "timer on this monitor?",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


def _no_object() -> Any | None:
    return None


def _false() -> bool:
    return False


def _nothing() -> None:
    return None


def default_secondary_screens() -> Sequence[Any]:
    return ScreenManager.secondary_screens()


@dataclass(frozen=True)
class ProjectionWindowContext:
    session: ProjectionSession
    font_manager: Any
    secondary_screens: Callable[[], Sequence[Any]]
    sync_projection_integrations: Callable[[], None]
    sync_obs_scene: Callable[[bool], None]
    yearly_text: Callable[[], tuple[str, str, str]]
    request_idle_media: Callable[[str], None]
    set_projection_screen_count: Callable[[int], None]
    set_toolbar_screen_count: Callable[[int], None]
    monitor_popup: Callable[[], Any | None]
    monitor_anchor: Callable[[], Any]
    dialog_parent: Any | None = None
    timer_output: Callable[[], Any | None] = _no_object
    timer_bridge: Callable[[], Any | None] = _no_object
    program_mirror_enabled: Callable[[], bool] = _false
    native_outputs_changed: Callable[[], None] = _nothing


class ProjectionWindowController:
    """Owns projection target windows and monitor-manager actions."""

    def __init__(self, context: ProjectionWindowContext) -> None:
        self._context = context
        self._session = context.session

    def _media_eligible(self, screen) -> bool:
        """True when media projection windows may be shown on ``screen``."""
        return self._session.media_eligible(screen)

    def _is_timer_reserved(self, screen) -> bool:
        return self._session.timer_reserved(screen)

    def _persist_media_owner(self, screen, active: bool) -> None:
        """Record an explicit media show/hide choice in the shared store."""
        self._session.set_media_owner(screen, active=active)

    def open_projection_windows(self) -> None:
        for w in self._session.projection_windows:
            w.close()
        self._session.projection_windows.clear()

        self._normalize_expired_state()

        secondary = self._context.secondary_screens()
        idx = 0
        for screen in secondary:
            if not self._media_eligible(screen):
                continue
            idx += 1
            win = ProjectionWindow(screen, idx, self._context.font_manager)
            self._session.projection_windows.append(win)

        for win in self._session.projection_windows:
            self.apply_full_state_to_window(win)

        self._set_screen_count(len(self._session.projection_windows))
        self._context.sync_projection_integrations()

    def apply_yearly_text(self, quote: str, ref: str, api_code: str = "") -> None:
        for win in self.all_windows():
            win.set_yearly_text(quote, ref, api_code)

    def apply_full_state_to_window(self, win) -> None:
        """Restore application-owned state; libobs owns the shared idle surface."""
        quote, ref, api_code = self._yearly_text()
        win.set_yearly_text(quote, ref, api_code)
        self.restore_state_to_window(win)

    def _normalize_expired_state(self) -> None:
        """Collapse a timer whose target has already passed back to idle.

        Kept separate from apply_full_state_to_window because it mutates the
        *shared* state once, not per-window.  Without it an expired countdown
        would be replayed (and silently ignored) on every new window forever.
        """
        state = self._session.state
        if state.get("type") != "timer":
            return
        now = QDateTime.currentDateTime()
        if now.secsTo(state["target_dt"]) <= 0:
            self._session.reset_state()
            self._context.sync_obs_scene(False)

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
        # Filter out any screens Qt has already deleted under us.
        secondary = [
            s for s in self._context.secondary_screens()
            if self._screen_name(s) is not None
        ]
        connected: dict[str, object] = {}
        for screen in secondary:
            name = self._screen_name(screen)
            if name is not None:
                connected[name] = screen

        still_valid: list[ProjectionWindow] = []
        for win in self._session.projection_windows:
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
        self._session.projection_windows = still_valid

        existing_names = {
            n for n in (
                self._screen_name(win.screen())
                for win in self._session.projection_windows
            )
            if n is not None
        }
        self._normalize_expired_state()

        for i, screen in enumerate(secondary):
            name = self._screen_name(screen)
            if name is None or name in existing_names:
                continue
            if not self._media_eligible(screen):
                continue
            win = ProjectionWindow(screen, i + 1, self._context.font_manager)
            self.apply_full_state_to_window(win)
            self._session.projection_windows.append(win)

        self._set_screen_count(len(self._session.projection_windows))
        self._context.sync_projection_integrations()

    def all_windows(self) -> list:
        return self._session.all_windows()

    def request_idle_media(self, request: object) -> None:
        """Confirm and apply one tree-originated idle-screen request."""

        if not isinstance(request, IdleMediaRequest):
            return
        media_type = str(request.media_type or "").strip().lower()
        path = existing_idle_media_path(media_type, request.path)
        if not path:
            return
        title = request.title or os.path.basename(path)
        pixmap = load_thumbnail_path(request.thumbnail_path)
        if pixmap is None and media_type == "image":
            pixmap = load_thumbnail_path(path)
        if confirm_set_as_idle(
            title,
            pixmap=pixmap,
            parent=self._context.dialog_parent,
        ):
            self.on_idle_media_changed(path)

    def on_idle_media_changed(self, path: str) -> None:
        """Prepare the requested choice in the engine before accepting it."""
        self._context.request_idle_media(path)
        # The popup emits an optimistic choice. Keep its UI on the confirmed
        # session value until the asynchronous engine update succeeds.
        self._sync_idle_popup()

    def on_idle_media_applied(self, _path: str) -> None:
        """Refresh UI from the session already confirmed by the scene runtime."""
        self._sync_idle_popup()

    def _sync_idle_popup(self) -> None:
        popup = self._context.monitor_popup()
        if popup is not None:
            popup._sync_idle_ui(self._session.idle_media_path)

    def on_floating_toggle(self, make_active: bool) -> None:
        if make_active:
            if self._session.floating_preview_window is None:
                self._session.floating_preview_window = self.create_floating_window()
        else:
            self._session.close_floating_preview()

        self._context.native_outputs_changed()
        self._context.sync_projection_integrations()
        QTimer.singleShot(420, self._reopen_monitor_manager)

    def create_floating_window(self) -> FloatingPreviewWindow:
        quote, ref, api_code = self._yearly_text()
        win = FloatingPreviewWindow(
            self._context.font_manager,
            yearly_text_quote=quote,
            yearly_text_ref=ref,
            api_code=api_code,
        )
        win.respawn_requested.connect(self.on_floating_respawn)
        self.apply_full_state_to_window(win)
        return win

    def on_floating_respawn(self, saved_geo) -> None:
        self._session.floating_preview_window = None
        self._context.native_outputs_changed()
        QTimer.singleShot(120, lambda: self.respawn_floating_silent(saved_geo))

    def respawn_floating_silent(self, saved_geo) -> None:
        if self._session.floating_preview_window is not None:
            return
        self._session.floating_preview_window = self.create_floating_window()
        self._context.native_outputs_changed()
        QTimer.singleShot(0, lambda: (
            self._session.floating_preview_window.setGeometry(saved_geo)
            if self._session.floating_preview_window is not None else None
        ))

    def on_monitor_manager_requested(self, anchor_widget) -> None:
        popup = self._context.monitor_popup()
        if popup is None:
            return
        secondary = self._context.secondary_screens()
        active_screens = {
            id(win.screen()): win for win in self._session.projection_windows
        }
        screens_info = []
        for i, screen in enumerate(secondary):
            screens_info.append({
                "screen": screen,
                "active": id(screen) in active_screens,
                "index": i,
                "timer_reserved": self._session.timer_reserved(screen),
            })

        floating_active = self._session.floating_preview_window is not None
        popup.populate(
            screens_info,
            floating_active=floating_active,
            idle_media_path=self._session.idle_media_path,
        )
        popup.show_above(anchor_widget)

    def on_monitor_toggle(self, screen_index: int, make_active: bool) -> None:
        secondary = self._context.secondary_screens()
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
                self._session.show_media_on_screen_name(screen_name)
                self._session.confirm_media_assignment(screen)
                self._release_timer_on(screen)
                self._notify_timer_monitors_changed()
                # Rebuild media windows through reconcile, which re-queries the
                # *current* screen list — the same authoritative path used for
                # hot-plug and "unreserve", and the reason this works where a
                # manual create from the now-stale `screen` did not.
                self.reconcile_projection_windows()
            else:
                self._session.show_media_on_screen_name(screen_name)
                self._persist_media_owner(screen, active=True)
                already = any(
                    win.screen() == screen
                    for win in self._session.projection_windows
                )
                if not already:
                    win = ProjectionWindow(
                        screen,
                        screen_index + 1,
                        self._context.font_manager,
                    )
                    self._session.projection_windows.append(win)
                    self.apply_full_state_to_window(win)
                    self._set_screen_count(len(self._session.projection_windows))
        else:
            self._session.hide_media_on_screen_name(screen_name)
            self._persist_media_owner(screen, active=False)
            to_remove = [
                win for win in self._session.projection_windows
                if win.screen() == screen
            ]
            for win in to_remove:
                self._session.projection_windows.remove(win)
                win.fade_out_and_close()
            self._set_screen_count(len(self._session.projection_windows))

        self._context.sync_projection_integrations()
        QTimer.singleShot(420, self._reopen_monitor_manager)

    def on_monitor_all(self, make_active: bool) -> None:
        if make_active:
            self._session.clear_hidden_media_screens()
            # "Project all" claims every non-timer-reserved screen for media.
            for screen in self._context.secondary_screens():
                if not self._is_timer_reserved(screen):
                    self._persist_media_owner(screen, active=True)
            self.open_projection_windows()
        else:
            for screen in self._context.secondary_screens():
                self._session.hide_media_on_screen_name(screen.name())
                self._persist_media_owner(screen, active=False)
            for win in self._session.projection_windows:
                win.fade_out_and_close()
            self._session.projection_windows.clear()
            self._set_screen_count(0)
            self._context.sync_projection_integrations()
        popup = self._context.monitor_popup()
        if popup is not None:
            popup.hide_animated()

    # ── Conflict handling (media side) ─────────────────────────────────────

    def _confirm_take_timer_screen(self, screen) -> bool:
        """Native confirmation before media displaces the timer on a screen."""
        from PySide6.QtWidgets import QMessageBox

        name = screen.name() or _tr(_THIS_MONITOR_SOURCE)
        box = QMessageBox(self._context.dialog_parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(_tr(_MONITOR_IN_USE_TITLE))
        box.setText(
            _tr(_TAKE_TIMER_MONITOR_SOURCE).format(monitor=name)
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _release_timer_on(self, screen) -> None:
        """Ask the timer-output controller to drop its window from ``screen``."""
        timer_output = self._context.timer_output()
        if timer_output is not None and hasattr(timer_output, "reconcile"):
            timer_output.reconcile()

    def _notify_timer_monitors_changed(self) -> None:
        """Refresh the Timer tab's monitor grid after a media-side takeover so
        the reserved monitor flips back to 'Reserve for timer'."""
        bridge = self._context.timer_bridge()
        if bridge is not None:
            bridge.refreshMonitors()

    def restore_state_to_window(self, win) -> None:
        if self._context.program_mirror_enabled():
            return
        state = self._session.state
        kind = state.get("type", "idle")
        if kind == "idle":
            win.clear()
            return
        if kind == "video":
            if state.get("is_audio", False):
                win.clear()
            else:
                win.begin_video()
        elif kind == "image":
            win.show_image_from_url_data(
                state["data"],
                initial_transform=(
                    image_transform_from_values(state.get("transform"))
                    or IDENTITY_IMAGE_TRANSFORM
                ),
            )
        elif kind == "timer":
            now = QDateTime.currentDateTime()
            remaining = now.secsTo(state["target_dt"])
            total = state.get("total", max(1, remaining))
            if remaining > 0:
                presentation = MediaCountdownPresentation(state["presentation"])
                win.show_timer(remaining, total, presentation)


    def _yearly_text(self) -> tuple[str, str, str]:
        return self._context.yearly_text()

    def _set_screen_count(self, count: int) -> None:
        self._context.set_projection_screen_count(count)
        self._context.set_toolbar_screen_count(count)
        self._context.native_outputs_changed()

    def _reopen_monitor_manager(self) -> None:
        self.on_monitor_manager_requested(self._context.monitor_anchor())
