from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QTimer

from ..core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE


AUTO_SHARE_REFOCUS_PROJECTION_DELAY_MS = 1900

log = logging.getLogger(__name__)


class AutoShareWorkerPool(Protocol):
    @property
    def is_stopped(self) -> bool: ...

    def submit(self, name: str, target: Callable[[], None]) -> None: ...

    def shutdown(self, timeout: float = 8.0) -> tuple[str, ...]: ...


class StartAutoShare(Protocol):
    def __call__(
        self,
        hotkey: str,
        click_x: int,
        click_y: int,
        *,
        movement_warning: Callable[[], None] | None = None,
    ) -> bool: ...


class DesktopAutomationInteractionGuard(Protocol):
    def acquire_automation_lock(self, owner: str) -> None: ...

    def release_automation_lock(self, owner: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ProjectionIntegrationContext:
    """Dependencies for synchronizing projection with external integrations."""

    projection_session: Any
    obs_scene_session: Any
    auto_key_projection: Any
    projection_windows: Callable[[], list[Any]]
    obs_service: Any
    obs_settings: Any
    auto_share_settings: Any
    projection_bar: Any
    interaction_guard: DesktopAutomationInteractionGuard
    auto_share_finished: Callable[[int, bool, bool], None]
    start_auto_share: StartAutoShare
    stop_auto_share: Callable[[str], bool]
    auto_share_mouse_interference_warning: Callable[[], None]
    raise_projection_window: Callable[[Any], None]
    auto_share_workers: AutoShareWorkerPool


class ProjectionIntegrationController:
    """Synchronizes projection state with OBS, Zoom, and auto-key edges."""

    def __init__(self, context: ProjectionIntegrationContext) -> None:
        self._context = context
        self._session = context.projection_session
        self._obs_scene_session = context.obs_scene_session
        self._auto_share_active = False
        self._auto_share_start_pending = False
        self._auto_share_playback_pending = False
        self._auto_share_generation = 0
        self._auto_share_interaction_locks: set[str] = set()

    def update_status(
        self,
        active: bool,
        _label: str = "",
        visual: bool = True,
        auto_keys_media: bool | None = None,
        sync_obs: bool = True,
    ) -> None:
        # Status text was removed from the sidebar; the projection edge still
        # drives OBS, Zoom, and auto-key integrations.
        if not active:
            self._context.auto_key_projection.set_visual_active(False)
        elif auto_keys_media is not None:
            self._context.auto_key_projection.set_visual_active(bool(auto_keys_media))
        if sync_obs:
            self.sync_obs_scene(active=active, visual=visual)
        self.sync_zoom_share(active=active, visual=visual)

    def has_visible_projection_output(self) -> bool:
        for win in self._context.projection_windows():
            try:
                if win.isVisible():
                    return True
            except RuntimeError:
                continue
        return False

    def current_projection_activity(self) -> tuple[bool, bool]:
        state = self._session.state
        kind = state.get("type", "idle")
        if kind == "idle":
            return False, True
        if kind == "video":
            return True, not bool(state.get("is_audio", False))
        return True, True

    def sync_projection_integrations(self) -> None:
        active, visual = self.current_projection_activity()
        self.sync_obs_scene(active=active, visual=visual)
        self.sync_zoom_share(active=active, visual=visual)

    def sync_obs_scene(self, active: bool | None = None, visual: bool = True) -> None:
        context = self._context
        if not context.obs_service.is_connected:
            return

        if active is None:
            active, visual = self.current_projection_activity()

        has_output = self.has_visible_projection_output()
        media_scene = context.obs_settings.media_window_scene()
        default_scene = context.obs_settings.default_scene()

        going_to_media = active and visual and has_output

        if going_to_media:
            if MEMORIZE_PRE_MEDIA_SCENE:
                current = context.obs_service.current_scene or ""
                if media_scene and current and current != media_scene:
                    self._obs_scene_session.remember(current)
            scene = media_scene
        else:
            current = context.obs_service.current_scene or ""
            if media_scene and current and current != media_scene:
                return
            if MEMORIZE_PRE_MEDIA_SCENE:
                scene = self._obs_scene_session.pre_media_scene or default_scene
            else:
                scene = default_scene
            self._obs_scene_session.clear()

        if scene and not scene.startswith("—"):
            context.obs_service.request_scene_change(scene)
            context.projection_bar.set_obs_scene_is_media(going_to_media)

    def auto_share_hotkey(self) -> str:
        return self._context.auto_share_settings.hotkey()

    def auto_share_configured(self) -> bool:
        return self._context.auto_share_settings.is_configured()

    def prepare_video_playback_for_auto_share(self) -> bool:
        """Hold a visual video while a required share start is unresolved."""
        context = self._context
        should_wait = (
            not context.auto_share_workers.is_stopped
            and context.auto_share_settings.is_configured()
            and self.has_visible_projection_output()
            and (not self._auto_share_active or self._auto_share_start_pending)
        )
        if not should_wait:
            return False

        self._auto_share_playback_pending = True
        context.projection_bar.begin_auto_share_playback_wait()
        return True

    def _resolve_auto_share_playback(self, success: bool) -> None:
        if not self._auto_share_playback_pending:
            return
        self._auto_share_playback_pending = False
        self._context.projection_bar.resolve_auto_share_playback_wait(success)

    def _cancel_auto_share_playback(self) -> None:
        if not self._auto_share_playback_pending:
            return
        self._auto_share_playback_pending = False
        self._context.projection_bar.cancel_auto_share_playback_wait()

    @staticmethod
    def _auto_share_lock_owner(generation: int) -> str:
        return f"zoom-auto-share:{generation}"

    def _acquire_auto_share_interaction_lock(self, generation: int) -> None:
        owner = self._auto_share_lock_owner(generation)
        self._context.interaction_guard.acquire_automation_lock(owner)
        self._auto_share_interaction_locks.add(owner)

    def _release_auto_share_interaction_lock(self, generation: int) -> None:
        owner = self._auto_share_lock_owner(generation)
        if owner not in self._auto_share_interaction_locks:
            return
        self._auto_share_interaction_locks.remove(owner)
        self._context.interaction_guard.release_automation_lock(owner)

    def _release_all_auto_share_interaction_locks(self) -> None:
        for owner in tuple(self._auto_share_interaction_locks):
            self._context.interaction_guard.release_automation_lock(owner)
        self._auto_share_interaction_locks.clear()

    def sync_zoom_share(self, active: bool | None = None, visual: bool = True) -> None:
        if self._context.auto_share_workers.is_stopped:
            self._resolve_auto_share_playback(False)
            return
        if active is None:
            active, visual = self.current_projection_activity()

        context = self._context
        if not context.auto_share_settings.is_configured():
            self._auto_share_generation += 1
            self._auto_share_active = False
            self._auto_share_start_pending = False
            self._resolve_auto_share_playback(False)
            return

        hotkey = self.auto_share_hotkey()
        should_share = active and visual and self.has_visible_projection_output()
        if should_share == self._auto_share_active:
            if not should_share:
                self._resolve_auto_share_playback(False)
            return
        self._auto_share_generation += 1
        generation = self._auto_share_generation

        if should_share:
            click_x, click_y = context.auto_share_settings.click_position()
            self._acquire_auto_share_interaction_lock(generation)

            def _run_start_share():
                try:
                    ok = context.start_auto_share(
                        hotkey,
                        click_x,
                        click_y,
                        movement_warning=context.auto_share_mouse_interference_warning,
                    )
                except Exception:  # noqa: BLE001 - automation worker boundary
                    log.exception("Automatic Zoom share start failed unexpectedly")
                    ok = False
                if not context.auto_share_workers.is_stopped:
                    try:
                        context.auto_share_finished(generation, True, ok)
                    except RuntimeError:
                        pass

            self._auto_share_active = True
            self._auto_share_start_pending = True
            try:
                self._launch_auto_share_worker("share-start", _run_start_share)
            except Exception:  # noqa: BLE001 - worker-pool lifecycle boundary
                log.exception("Could not launch automatic Zoom share start")
                self._release_auto_share_interaction_lock(generation)
                self._auto_share_active = False
                self._auto_share_start_pending = False
                self._resolve_auto_share_playback(False)
        else:
            self._auto_share_start_pending = False
            self._resolve_auto_share_playback(False)

            def _run_stop_share():
                ok = context.stop_auto_share(hotkey)
                if not context.auto_share_workers.is_stopped:
                    try:
                        context.auto_share_finished(generation, False, ok)
                    except RuntimeError:
                        pass

            self._auto_share_active = False
            self._launch_auto_share_worker("share-stop", _run_stop_share)

    def _launch_auto_share_worker(self, name: str, target: Callable[[], None]) -> None:
        self._context.auto_share_workers.submit(name, target)

    def cleanup(self, timeout: float = 8.0) -> None:
        """Suppress late callbacks and join owned auto-share workers."""
        self._auto_share_generation += 1
        self._auto_share_active = False
        self._auto_share_start_pending = False
        self._cancel_auto_share_playback()
        alive = self._context.auto_share_workers.shutdown(timeout)
        self._release_all_auto_share_interaction_locks()
        if alive:
            log.warning("Auto-share workers still running during shutdown: %s", alive)

    def on_auto_share_finished(
        self,
        generation: int,
        target_active: bool,
        ok: bool,
    ) -> None:
        self._release_auto_share_interaction_lock(generation)
        if generation != self._auto_share_generation:
            return
        if target_active:
            self._auto_share_start_pending = False
            self._resolve_auto_share_playback(ok)
        if not ok:
            self._auto_share_active = not target_active
            return
        self._auto_share_active = target_active
        if target_active:
            QTimer.singleShot(
                AUTO_SHARE_REFOCUS_PROJECTION_DELAY_MS,
                self.raise_visible_projection_windows,
            )

    def raise_visible_projection_windows(self) -> None:
        for win in list(self._session.projection_windows):
            try:
                if not win.isVisible():
                    continue
                self._context.raise_projection_window(win)
            except RuntimeError:
                continue
