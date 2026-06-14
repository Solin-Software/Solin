"""Monitor reservation adapter for timer presentation."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..core.ui.monitor_allocation import OWNER_MEDIA, OWNER_OFF, OWNER_TIMER


class TimerMonitorController:
    def __init__(
        self,
        *,
        screen_manager,
        allocation,
        timer_output,
        projection_windows: Callable[[], Sequence],
        reconcile_media: Callable[[], None],
        deactivate_media_screen: Callable[[str], None],
    ) -> None:
        self._screen_manager = screen_manager
        self._allocation = allocation
        self._output = timer_output
        self._projection_windows = projection_windows
        self._reconcile_media = reconcile_media
        self._deactivate_media_screen = deactivate_media_screen

    def model(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index, screen in enumerate(self._secondary_screens()):
            owner = self._allocation.owner_of(screen)
            geometry = screen.geometry()
            rows.append(
                {
                    "index": index,
                    "name": screen.name() or f"Monitor {index + 1}",
                    "resolution": (
                        f"{geometry.width()} × {geometry.height()}"
                    ),
                    "owner": owner,
                    "reserved": owner == OWNER_TIMER,
                    "usedByMedia": owner == OWNER_MEDIA,
                }
            )
        return rows

    def request_reserve(
        self,
        index: int,
        fallback_name: str,
    ) -> dict[str, object]:
        screen = self._screen_at(index)
        if screen is None:
            return {}
        if self._media_present_on(screen):
            return {
                "conflict": True,
                "index": index,
                "screenName": screen.name() or fallback_name,
            }
        self._reserve(screen)
        return {}

    def confirm_reserve(self, index: int) -> bool:
        screen = self._screen_at(index)
        if screen is None:
            return False
        self._reserve(screen)
        return True

    def unreserve(self, index: int) -> bool:
        screen = self._screen_at(index)
        if screen is None:
            return False
        self._allocation.set_owner(screen, OWNER_OFF)
        self._deactivate_media_screen(screen.name())
        self._output.reconcile()
        return True

    def set_visible(self, visible: bool) -> None:
        self._output.set_visible(visible)

    def is_visible(self) -> bool:
        return self._output.is_visible()

    def _reserve(self, screen) -> None:
        self._allocation.set_owner(screen, OWNER_TIMER)
        self._reconcile_media()
        self._output.reconcile()

    def _secondary_screens(self) -> list:
        return list(self._screen_manager.secondary_screens())

    def _screen_at(self, index: int):
        screens = self._secondary_screens()
        if index < 0 or index >= len(screens):
            return None
        return screens[index]

    def _media_present_on(self, screen) -> bool:
        for window in self._projection_windows():
            try:
                if window.screen() == screen:
                    return True
            except Exception:  # noqa: BLE001 - Qt screen-lifecycle boundary
                continue
        return False
