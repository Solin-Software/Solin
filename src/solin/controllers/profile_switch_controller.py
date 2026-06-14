from __future__ import annotations


class ProfileSwitchController:
    """Emits profile-switch requests from the MainWindow shell."""

    def __init__(self, window) -> None:
        self._window = window

    def request_switch(self) -> None:
        self._window.switch_profile_requested.emit()
