"""Framework-independent onboarding workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from solin.core.profiles.models import ProfileInfo, normalize_profile_name


@dataclass(frozen=True, slots=True)
class OBSOnboardingConfiguration:
    enabled: bool = False
    port: int = 4455
    password: str = ""
    default_scene: str = ""
    media_scene: str = ""

    def __post_init__(self) -> None:
        if self.enabled and not 1 <= self.port <= 65535:
            raise ValueError("OBS WebSocket port must be between 1 and 65535.")


@dataclass(frozen=True, slots=True)
class ProfileOnboardingCommand:
    name: str
    interface_language: str
    media_language: str = ""
    obs: OBSOnboardingConfiguration = field(
        default_factory=OBSOnboardingConfiguration
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_profile_name(self.name))
        object.__setattr__(
            self,
            "interface_language",
            self.interface_language.strip() or "en",
        )
        object.__setattr__(self, "media_language", self.media_language.strip())


class OnboardingSettings(Protocol):
    def apply(
        self,
        profile_id: str,
        command: ProfileOnboardingCommand,
    ) -> None: ...


class ProfileOnboardingLifecycle(Protocol):
    def create_profile(self, name: str) -> ProfileInfo: ...

    def set_active(self, profile_id: str) -> None: ...

    def rollback_profile_creation(self, profile_id: str) -> None: ...


class OnboardingService:
    def __init__(
        self,
        profiles: ProfileOnboardingLifecycle,
        settings: OnboardingSettings,
    ) -> None:
        self._profiles = profiles
        self._settings = settings

    def complete(self, command: ProfileOnboardingCommand) -> ProfileInfo:
        profile = self._profiles.create_profile(command.name)
        try:
            self._settings.apply(profile.id, command)
            self._profiles.set_active(profile.id)
        except Exception:  # noqa: BLE001 - onboarding adapter transaction boundary
            self._profiles.rollback_profile_creation(profile.id)
            raise
        return profile
