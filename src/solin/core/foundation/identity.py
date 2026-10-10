"""
identity.py
===========
Generate and persist an anonymous UUID identifying this Solin installation.

Purpose:
    Correlate consecutive update checks from the same device to provide
    accurate server-side version metrics:
      • Distribution of versions in use
      • Confirmed upgrades (same ID, new version)
      • Active instance counts unaffected by IP address changes

Privacy:
    - A random UUID generated with uuid4() on the first run.
    - Contains no username, email, HWID, MAC address, or other personal data.
    - Can be reset by clearing Solin settings (QSettings).

GLOBAL scope (not per profile):
    install_id identifies the installation/device, not a user profile.
    It deliberately uses the global "<base>"/"App" namespace so that all
    profiles on the same machine share the same ID. In development,
    <base> is "SolinDev"; in production, "Solin", keeping test telemetry
    and configuration separate from the production installation.

Usage:
    from solin.core.foundation.identity import get_install_id
    install_id = get_install_id(settings)  # str, 32 hex characters without separators
"""
from __future__ import annotations

import uuid
import logging
from typing import Protocol


log = logging.getLogger(__name__)


class InstallationIdentitySettings(Protocol):
    def install_id(self) -> str:
        ...

    def set_install_id(self, install_id: str) -> None:
        ...


def get_install_id(settings: InstallationIdentitySettings) -> str:
    """
    Return the persisted install_id, generating one if it does not exist.
    Always use the global installation namespace, not the profile namespace.
    """
    existing = settings.install_id()

    if existing and len(existing) >= 32:
        return existing

    # First run: generate and persist a random UUID.
    new_id = uuid.uuid4().hex   # 32 hexadecimal characters without separators
    settings.set_install_id(new_id)
    log.debug("[DeviceID] new install_id generated: %s...", new_id[:8])
    return new_id
