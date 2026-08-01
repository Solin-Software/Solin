"""Profile-scoped persistence for the virtual-camera scene configuration.

Stores the operator's :class:`VcamSceneConfig` (meeting mode + per-mode rule
overrides + PiP placement) as a single JSON value in the profile's ``QSettings``
prefs namespace — the same store other feature settings use. This is the vcam
composition config, NOT the OBS-remote "scenes" (see :mod:`vcam_model`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..foundation.constants import QSETTINGS_PREFS_APP
from ..foundation.settings_keys import SettingsKey
from ..foundation.settings_store import SettingsStore
from ..profiles.settings import ProfileSettings
from .vcam_model import VcamSceneConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VcamSettingsStore:
    """Reads/writes the :class:`VcamSceneConfig` for one profile."""

    settings: SettingsStore

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings) -> "VcamSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization, QSETTINGS_PREFS_APP
            )
        )

    def scene_config(self) -> VcamSceneConfig:
        """The persisted config, or the curated defaults when unset/corrupt."""
        raw = self.settings.string(SettingsKey.VCAM_SCENE_CONFIG, "")
        if not raw:
            return VcamSceneConfig()
        try:
            return VcamSceneConfig.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            log.debug("Ignoring corrupt vcam scene config", exc_info=True)
            return VcamSceneConfig()

    def save_scene_config(self, config: VcamSceneConfig) -> None:
        self.settings.set_value(
            SettingsKey.VCAM_SCENE_CONFIG, json.dumps(config.to_dict())
        )


__all__ = ["VcamSettingsStore"]
