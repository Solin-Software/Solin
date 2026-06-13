from __future__ import annotations


class ProfileSwitchController:
    """Connects the MainWindow profile avatar to the active profile state."""

    def __init__(self, window, profile_manager) -> None:
        self._window = window
        self._profile_manager = profile_manager

    def request_switch(self) -> None:
        self._window.switch_profile_requested.emit()

    def update_avatar(self, _profile_id: str = "") -> None:
        active = self._profile_manager.active_profile
        avatar = getattr(self._window, "_profile_avatar_btn", None)
        if active and avatar is not None:
            avatar.set_name(active.name)
