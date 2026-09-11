"""Strict, versioned release metadata shared by publisher and consumer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from .version import ReleaseVersion

RELEASE_REPOSITORY = "Solin-Software/Solin"
MANIFEST_FILENAME = "release-manifest.json"
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 2_000_000
# Exact distribution inventory; diagnostic output is never eligible.
REQUIRED_ASSETS = {
    ("windows", "x86_64", "installer"): ("exe", "10.0.17763"),
    ("macos", "x86_64", "dmg"): ("dmg", "13.0"),
    ("macos", "arm64", "dmg"): ("dmg", "13.0"),
    ("linux", "x86_64", "appimage"): ("AppImage", "2.35"),
}
_HASH = re.compile(r"^[a-f0-9]{64}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_LOCALE = re.compile(r"^[a-z]{2,3}(?:_[A-Za-z0-9]{2,8})*$")


def asset_filename(version: ReleaseVersion, platform: str, architecture: str, kind: str) -> str:
    extension, _ = REQUIRED_ASSETS[(platform, architecture, kind)]
    return f"Solin-{version.display_version}-{platform}-{architecture}.{extension}"


def release_asset_url(tag: str, filename: str) -> str:
    return (
        f"https://github.com/{RELEASE_REPOSITORY}/releases/download/"
        f"{quote(tag, safe='')}/{quote(filename, safe='')}"
    )


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    platform: str
    architecture: str
    kind: str
    filename: str
    size: int
    sha256: str
    minimum_os: str

    def download_url(self, tag: str) -> str:
        return release_asset_url(tag, self.filename)


@dataclass(frozen=True, slots=True)
class LocalizedReleaseNotes:
    version: ReleaseVersion
    translations: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: ReleaseVersion
    tag: str
    channel: str
    commit_sha: str
    assets: tuple[ReleaseAsset, ...]
    notes: tuple[LocalizedReleaseNotes, ...]

    @classmethod
    def parse(cls, payload: object) -> ReleaseManifest | None:
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
            return None
        if payload["schema_version"] != SCHEMA_VERSION:
            return None
        version = ReleaseVersion.parse(payload.get("version"))
        if version is None or ReleaseVersion.from_tag(str(payload.get("tag", ""))) != version:
            return None
        if payload.get("version") != version.canonical or payload.get("channel") != version.channel:
            return None
        commit = payload.get("commit_sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            return None
        raw_assets, raw_notes = payload.get("assets"), payload.get("notes")
        if not isinstance(raw_assets, list) or not isinstance(raw_notes, list):
            return None
        if len(raw_assets) != len(REQUIRED_ASSETS) or not 1 <= len(raw_notes) <= 2000:
            return None
        assets: list[ReleaseAsset] = []
        seen: set[tuple[str, str, str]] = set()
        for item in raw_assets:
            if not isinstance(item, dict):
                return None
            key = (item.get("platform"), item.get("architecture"), item.get("kind"))
            if not all(isinstance(part, str) for part in key):
                return None
            if key not in REQUIRED_ASSETS or key in seen:
                return None
            if item.get("filename") != asset_filename(version, *key):
                return None
            size, digest = item.get("size"), item.get("sha256")
            if type(size) is not int or not 0 < size <= 2 * 1024**3:
                return None
            if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                return None
            if item.get("minimum_os") != REQUIRED_ASSETS[key][1]:
                return None
            assets.append(ReleaseAsset(*key, item["filename"], size, digest, item["minimum_os"]))
            seen.add(key)
        notes: list[LocalizedReleaseNotes] = []
        seen_versions: set[ReleaseVersion] = set()
        for item in raw_notes:
            if not isinstance(item, dict):
                return None
            note_version = ReleaseVersion.parse(item.get("version"))
            translations = item.get("translations")
            if note_version is None or note_version > version or note_version in seen_versions:
                return None
            if not isinstance(translations, dict) or not translations.get("en"):
                return None
            if not all(
                isinstance(locale, str) and _LOCALE.fullmatch(locale)
                and isinstance(markdown, str) and markdown.strip() and len(markdown) <= 200_000
                for locale, markdown in translations.items()
            ):
                return None
            if not version.is_beta and note_version.is_beta:
                return None
            notes.append(LocalizedReleaseNotes(note_version, dict(translations)))
            seen_versions.add(note_version)
        if version not in seen_versions:
            return None
        return cls(version, version.tag, version.channel, commit, tuple(assets), tuple(notes))

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {
            "schema_version": SCHEMA_VERSION,
            "version": self.version.canonical,
            "tag": self.tag,
            "channel": self.channel,
            "commit_sha": self.commit_sha,
            "assets": [asdict(asset) for asset in self.assets],
            "notes": [
                {"version": note.version.canonical, "translations": note.translations}
                for note in self.notes
            ],
        }
