"""Pure selection of compatible GitHub releases and localized release notes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from packaging.version import InvalidVersion, Version

from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import ReleaseAsset, ReleaseManifest
from solin.core.releases.version import ReleaseVersion


class UpdateAction(StrEnum):
    INSTALL = "install"
    OPEN = "open"
    REVEAL = "reveal"


@dataclass(frozen=True, slots=True)
class ReleaseNote:
    version: ReleaseVersion
    markdown: str
    language: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    action: UpdateAction
    version: ReleaseVersion
    asset: ReleaseAsset
    url: str
    changelog: tuple[ReleaseNote, ...] = ()


def evaluate_update(
    manifests: list[ReleaseManifest],
    *,
    current_version: str | ReleaseVersion,
    channel: UpdateChannel = UpdateChannel.STABLE,
    platform: str,
    architecture: str,
    os_version: str,
    installed_windows: bool = False,
    automatic_windows: bool = True,
    language: str = "en",
) -> UpdateInfo | None:
    current = (
        current_version
        if isinstance(current_version, ReleaseVersion)
        else ReleaseVersion.parse(current_version)
    )
    if current is None:
        return None
    kind = {
        "windows": "installer",
        "macos": "dmg",
        "linux": "appimage",
    }.get(platform)
    eligible = [
        m
        for m in manifests
        if m.version > current and (channel == UpdateChannel.BETA or not m.version.is_beta)
    ]
    for manifest in sorted(eligible, key=lambda m: m.version, reverse=True):
        for asset in manifest.assets:
            if (asset.platform, asset.architecture, asset.kind) != (platform, architecture, kind):
                continue
            try:
                if asset.minimum_os and Version(os_version) < Version(asset.minimum_os):
                    continue
            except InvalidVersion:
                continue
            notes: dict[ReleaseVersion, ReleaseNote] = {}
            for release in eligible:
                for note in release.notes:
                    if not current < note.version <= manifest.version:
                        continue
                    if note.version.is_beta:
                        # A final release's consolidated notes supersede its betas.
                        base = note.version.canonical.split("b", 1)[0]
                        if any(
                            m.version.canonical == base
                            for m in eligible
                            if m.version <= manifest.version
                        ):
                            continue
                        if channel == UpdateChannel.STABLE:
                            continue
                    locale = language if language in note.translations else "en"
                    notes.setdefault(
                        note.version, ReleaseNote(note.version, note.translations[locale], locale)
                    )
            action = (
                UpdateAction.INSTALL
                if platform == "windows" and installed_windows and automatic_windows
                else UpdateAction.OPEN
                if platform == "macos"
                else UpdateAction.REVEAL
            )
            return UpdateInfo(
                action,
                manifest.version,
                asset,
                asset.download_url(manifest.tag),
                tuple(notes[v] for v in sorted(notes, reverse=True)),
            )
    return None
