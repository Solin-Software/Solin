from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import unicodedata
from typing import Final

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.remote_control.certificates import parse_private_lan_ipv4


REMOTE_CONTROL_PORT: Final = 8765
_NETWORK_SELECTION_VERSION: Final = 1
_INTERFACE_IDENTIFIER_MAX_LENGTH: Final = 256


def validate_interface_identifier(identifier: str) -> str:
    if not isinstance(identifier, str):
        raise ValueError("Network interface identifier must be text")
    normalized = unicodedata.normalize("NFKC", identifier).strip()
    if not normalized or len(normalized) > _INTERFACE_IDENTIFIER_MAX_LENGTH:
        raise ValueError("Network interface identifier must contain 1 to 256 characters")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise ValueError("Network interface identifier cannot contain control characters")
    return normalized


@dataclass(frozen=True, slots=True)
class NetworkInterfaceSelection:
    interface_identifier: str
    ipv4_address: IPv4Address

    @property
    def selection_key(self) -> str:
        return f"{self.interface_identifier}|{self.ipv4_address}"

    @classmethod
    def create(
        cls,
        interface_identifier: str,
        ipv4_address: str | IPv4Address,
    ) -> NetworkInterfaceSelection:
        return cls(
            interface_identifier=validate_interface_identifier(interface_identifier),
            ipv4_address=parse_private_lan_ipv4(ipv4_address),
        )

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": _NETWORK_SELECTION_VERSION,
                "interface_identifier": self.interface_identifier,
                "ipv4_address": str(self.ipv4_address),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, raw: str) -> NetworkInterfaceSelection | None:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(value, dict) or value.get("version") != _NETWORK_SELECTION_VERSION:
            return None
        try:
            return cls.create(
                value["interface_identifier"],
                value["ipv4_address"],
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class RemoteControlSettingsSnapshot:
    enabled: bool
    network_selection: NetworkInterfaceSelection | None


@dataclass(frozen=True, slots=True)
class RemoteControlSettingsStore:
    """Typed profile-scoped settings for the fixed-port remote-control service."""

    profile_settings: ProfileAppSettingsStore

    @classmethod
    def create(cls, profile_settings: ProfileAppSettingsStore) -> RemoteControlSettingsStore:
        return cls(profile_settings)

    def snapshot(self) -> RemoteControlSettingsSnapshot:
        return RemoteControlSettingsSnapshot(
            enabled=self.enabled(),
            network_selection=self.network_selection(),
        )

    def enabled(self) -> bool:
        return bool(
            self.profile_settings.settings.value(
                SettingsKey.REMOTE_CONTROL_ENABLED,
                False,
                bool,
            )
        )

    def set_enabled(self, enabled: bool) -> None:
        self.profile_settings.settings.set_value(
            SettingsKey.REMOTE_CONTROL_ENABLED,
            bool(enabled),
        )

    def onboarding_seen(self) -> bool:
        return bool(
            self.profile_settings.settings.value(
                SettingsKey.REMOTE_CONTROL_ONBOARDING_SEEN,
                False,
                bool,
            )
        )

    def mark_onboarding_seen(self) -> None:
        self.profile_settings.settings.set_value(
            SettingsKey.REMOTE_CONTROL_ONBOARDING_SEEN,
            True,
        )

    def network_selection(self) -> NetworkInterfaceSelection | None:
        raw = self.profile_settings.settings.string(SettingsKey.REMOTE_CONTROL_NETWORK_SELECTION)
        return NetworkInterfaceSelection.deserialize(raw) if raw else None

    def set_network_selection(
        self,
        interface_identifier: str,
        ipv4_address: str | IPv4Address,
    ) -> None:
        selection = NetworkInterfaceSelection.create(interface_identifier, ipv4_address)
        self.profile_settings.settings.set_value(
            SettingsKey.REMOTE_CONTROL_NETWORK_SELECTION,
            selection.serialize(),
        )

    def clear_network_selection(self) -> None:
        self.profile_settings.settings.remove(SettingsKey.REMOTE_CONTROL_NETWORK_SELECTION)
