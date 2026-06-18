from __future__ import annotations

from collections.abc import Callable


class ProfileSwitchController:
    """Emits profile-switch requests from the MainWindow shell."""

    def __init__(self, request_profile_switch: Callable[[], None]) -> None:
        self._request_profile_switch = request_profile_switch

    def request_switch(self) -> None:
        self._request_profile_switch()
