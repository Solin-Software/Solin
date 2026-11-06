"""Package selection and system requirements shared by release consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packaging.version import InvalidVersion, Version

from .manifest import ReleaseAsset


class ReleaseCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    SYSTEM_TOO_OLD = "system_too_old"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    platform: str
    architecture: str
    os_version: str

    def matching_asset(self, assets: tuple[ReleaseAsset, ...]) -> ReleaseAsset | None:
        kind = {"windows": "installer", "macos": "dmg", "linux": "appimage"}.get(self.platform)
        return next(
            (
                asset
                for asset in assets
                if (asset.platform, asset.architecture, asset.kind)
                == (self.platform, self.architecture, kind)
            ),
            None,
        )

    def compatibility(self, asset: ReleaseAsset | None) -> ReleaseCompatibility:
        if asset is None or self.matching_asset((asset,)) is None:
            return ReleaseCompatibility.UNAVAILABLE
        try:
            minimum, detected = Version(asset.minimum_os), Version(self.os_version)
        except InvalidVersion:
            return ReleaseCompatibility.UNVERIFIED
        return (
            ReleaseCompatibility.SYSTEM_TOO_OLD
            if detected < minimum
            else ReleaseCompatibility.COMPATIBLE
        )
