"""Application state for media projection surfaces and monitor ownership."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import logging
from typing import Any, Protocol

from solin.core.projection.monitor_allocation import (
    OWNER_MEDIA,
    OWNER_OFF,
    OWNER_TIMER,
)


ProjectionState = dict[str, Any]
log = logging.getLogger(__name__)


class MonitorAllocation(Protocol):
    def owner_of(self, screen) -> str: ...

    def set_owner(self, screen, owner: str) -> None: ...

    def confirm_assignment(self, screen, new_owner: str) -> None: ...


def idle_projection_state() -> ProjectionState:
    return {"type": "idle"}


def projection_presentation_type(state: Mapping[str, Any]) -> str:
    """Return the visual presentation represented by projection state.

    Audio playback is an active transport session, but it does not replace the
    content shown on projection surfaces. Those surfaces continue presenting
    idle while the audio-specific controls and playback state remain active.
    """

    state_type = str(state.get("type", "idle"))
    if state_type == "video" and bool(state.get("is_audio", False)):
        return "idle"
    return state_type


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
        self._revision = 0
        self._session_id = 0
        self._presentation_session_id = 0
        self._image_transform_animate = False
        # Projection observers form an ordered application pipeline: content
        # identity/framing is published before scene reconciliation, which in
        # turn precedes surface routing. A set made that order hash-dependent
        # and turned first-frame scene changes into an intermittent race.
        self._listeners: dict[Callable[[], None], None] = {}
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

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def session_id(self) -> int:
        """Monotonic identity of the active projection, excluding state updates."""
        return self._session_id

    @property
    def presentation_session_id(self) -> int:
        """Identity of the content currently shown on visual outputs.

        Unlike ``session_id``, this remains stable across idle-to-audio and
        audio-to-idle changes because audio owns transport, not presentation.
        """

        return self._presentation_session_id

    def set_state(self, state: Mapping[str, Any]) -> None:
        previous_presentation = projection_presentation_type(self._state)
        self._state = dict(state)
        self._image_transform_animate = False
        self._session_id += 1
        current_presentation = projection_presentation_type(self._state)
        if previous_presentation != "idle" or current_presentation != "idle":
            self._presentation_session_id += 1
        self._publish_changed()

    def update_state(self, **changes: Any) -> None:
        if not changes or all(self._state.get(key) == value for key, value in changes.items()):
            return
        previous_presentation = projection_presentation_type(self._state)
        self._state.update(changes)
        if projection_presentation_type(self._state) != previous_presentation:
            self._presentation_session_id += 1
        self._publish_changed()

    @property
    def image_transform_animate(self) -> bool:
        """Animation intent for the latest image-transform state update."""

        return self._image_transform_animate

    def update_image_transform(
        self,
        transform: tuple[float, float, float],
        *,
        animate: bool,
    ) -> None:
        """Publish transform state while keeping animation intent transient."""

        self._image_transform_animate = bool(animate)
        self.update_state(transform=transform)

    def reset_state(self) -> None:
        previous_presentation = projection_presentation_type(self._state)
        self._state = idle_projection_state()
        self._image_transform_animate = False
        self._session_id += 1
        if previous_presentation != "idle":
            self._presentation_session_id += 1
        self._publish_changed()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners[listener] = None

        def unsubscribe() -> None:
            self._listeners.pop(listener, None)

        return unsubscribe

    def _publish_changed(self) -> None:
        self._revision += 1
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - projection observer boundary
                log.warning("Projection state listener failed", exc_info=True)

    @property
    def idle_media_path(self) -> str:
        return self._idle_media_path

    def set_idle_media_path(self, path: str) -> None:
        self._idle_media_path = path

    @property
    def tab_projection_active(self) -> bool:
        return self._tab_projection_active

    def set_tab_projection_active(self, active: bool) -> None:
        normalized = bool(active)
        if normalized == self._tab_projection_active:
            return
        self._tab_projection_active = normalized
        self._publish_changed()

    def all_windows(self) -> list[Any]:
        windows = list(self.projection_windows)
        if self.floating_preview_window is not None:
            windows.append(self.floating_preview_window)
        return windows

    def has_active_projection(self) -> bool:
        """Return whether replacing media would displace an operator projection."""

        return self._tab_projection_active or self.state_type != "idle"

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
