"""Application state for media projection surfaces and monitor ownership."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from solin.core.ui.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER


ProjectionState = dict[str, Any]


class MonitorAllocation(Protocol):
    def owner_of(self, screen) -> str: ...

    def set_owner(self, screen, owner: str) -> None: ...

    def confirm_assignment(self, screen, new_owner: str) -> None: ...


def idle_projection_state() -> ProjectionState:
    return {"type": "idle"}


class ObsSceneSession:
    """State shared by projection flows that temporarily switch OBS scenes."""

    def __init__(self) -> None:
        self._pre_media_scene = ""

    @property
    def pre_media_scene(self) -> str:
        return self._pre_media_scene

    def remember(self, scene_name: str) -> None:
        self._pre_media_scene = scene_name

    def clear(self) -> None:
        self._pre_media_scene = ""


class ProjectionSession:
    """Single source of truth for projection state owned by one main window."""

    def __init__(
        self,
        *,
        allocation: MonitorAllocation | None = None,
        media_hidden_screen_names: Iterable[str] = (),
    ) -> None:
        self._allocation = allocation
        self._state: ProjectionState = idle_projection_state()
        self._idle_media_path = ""
        self._tab_projection_active = False
        self._media_hidden_screen_names = set(media_hidden_screen_names)
        self.projection_windows: list[Any] = []
        self.floating_preview_window: Any | None = None

    @property
    def state(self) -> ProjectionState:
        return self._state

    @property
    def state_type(self) -> str:
        return str(self._state.get("type", "idle"))

    def set_state(self, state: Mapping[str, Any]) -> None:
        self._state = dict(state)

    def reset_state(self) -> None:
        self._state = idle_projection_state()

    @property
    def idle_media_path(self) -> str:
        return self._idle_media_path

    def set_idle_media_path(self, path: str) -> None:
        self._idle_media_path = path

    @property
    def tab_projection_active(self) -> bool:
        return self._tab_projection_active

    def set_tab_projection_active(self, active: bool) -> None:
        self._tab_projection_active = bool(active)

    def all_windows(self) -> list[Any]:
        windows = list(self.projection_windows)
        if self.floating_preview_window is not None:
            windows.append(self.floating_preview_window)
        return windows

    def close_floating_preview(self) -> None:
        if self.floating_preview_window is not None:
            self.floating_preview_window.close()
            self.floating_preview_window = None

    def media_eligible(self, screen) -> bool:
        if self._allocation is None:
            return self._screen_name(screen) not in self._media_hidden_screen_names
        return self._allocation.owner_of(screen) == OWNER_MEDIA

    def timer_reserved(self, screen) -> bool:
        return self._allocation is not None and self._allocation.owner_of(screen) == OWNER_TIMER

    def set_media_owner(self, screen, *, active: bool) -> None:
        if self._allocation is None:
            return
        if self._allocation.owner_of(screen) == OWNER_TIMER:
            return
        self._allocation.set_owner(screen, OWNER_MEDIA if active else OWNER_OFF)

    def confirm_media_assignment(self, screen) -> None:
        if self._allocation is not None:
            self._allocation.confirm_assignment(screen, OWNER_MEDIA)

    def hide_media_on_screen_name(self, screen_name: str) -> None:
        self._media_hidden_screen_names.add(screen_name)

    def show_media_on_screen_name(self, screen_name: str) -> None:
        self._media_hidden_screen_names.discard(screen_name)

    def clear_hidden_media_screens(self) -> None:
        self._media_hidden_screen_names.clear()

    @property
    def media_hidden_screen_names(self) -> set[str]:
        return set(self._media_hidden_screen_names)

    @staticmethod
    def _screen_name(screen) -> str:
        try:
            return str(screen.name())
        except Exception:  # noqa: BLE001 - defensive screen capability boundary
            return ""
