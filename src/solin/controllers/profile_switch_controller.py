from __future__ import annotations

from collections.abc import Callable


class ProfileSwitchController:
    """Emits profile-switch requests from the MainWindow shell."""

    def __init__(
        self,
        request_profile_switch: Callable[[], None],
        *,
        can_switch: Callable[[], bool] | None = None,
        notify_blocked: Callable[[], None] | None = None,
    ) -> None:
        self._request_profile_switch = request_profile_switch
        self._can_switch = can_switch or (lambda: True)
        self._notify_blocked = notify_blocked

    def request_switch(self) -> bool:
        if not self._can_switch():
            if self._notify_blocked is not None:
                self._notify_blocked()
            return False
        self._request_profile_switch()
        return True
