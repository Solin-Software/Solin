"""Bounded, anonymous GitHub discovery with conditional persistent caching."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

from solin.core.network.http import HttpError, HttpStatusError, stream_get
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    MAX_MANIFEST_BYTES,
    MANIFEST_FILENAME,
    RELEASE_REPOSITORY,
    ReleaseManifest,
    release_asset_url,
)
from solin.core.releases.version import ReleaseVersion


class GitHubReleases:
    def __init__(self, cache_directory: Path, cancelled: Event | None = None) -> None:
        self._directory = cache_directory
        self._cancelled = cancelled or Event()
        self._deadline = 0.0
        self._cache: dict = {}
        try:
            payload = json.loads((cache_directory / "github.json").read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._cache = payload
        except (OSError, ValueError):
            pass

    def _check(self) -> float:
        remaining = self._deadline - time.monotonic()
        if self._cancelled.is_set() or remaining <= 0:
            raise HttpError("Update check cancelled or timed out")
        return remaining

    def _get(self, url: str) -> object:
        remaining = self._check()
        entry = self._cache.get(url, {})
        if not isinstance(entry, dict):
            entry = {}
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Solin-update-check"}
        if isinstance(entry.get("etag"), str):
            headers["If-None-Match"] = entry["etag"]
        try:
            with stream_get(url, timeout=min(5.0, remaining), headers=headers) as response:
                if response.status_code == 304:
                    if "payload" not in entry:
                        raise HttpError("Conditional response without cached release")
                    return entry["payload"]
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    self._check()
                    chunks.extend(chunk)
                    if len(chunks) > MAX_MANIFEST_BYTES:
                        raise HttpError("Release metadata exceeds size limit")
                payload = json.loads(chunks)
                self._cache[url] = {"etag": response.headers.get("ETag", ""), "payload": payload}
                return payload
        except HttpStatusError as exc:
            if exc.status_code in (403, 429):
                # Never retry within the same run; unauthenticated quota is hourly.
                self._cache["retry_after"] = time.time() + 3600
            raise

    def discover(self, current_version: str, channel: UpdateChannel) -> list[ReleaseManifest]:
        self._deadline = time.monotonic() + 20.0
        retry_after = self._cache.get("retry_after", 0)
        if isinstance(retry_after, (float, int)) and retry_after > time.time():
            return []
        current = ReleaseVersion.parse(current_version)
        if current is None:
            return []
        result = []
        try:
            for page in range(1, 11):
                releases = self._get(
                    f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases?per_page=100&page={page}"
                )
                if not isinstance(releases, list):
                    raise HttpError("Invalid GitHub releases response")
                for release in releases:
                    if not isinstance(release, dict) or release.get("draft") is not False:
                        continue
                    tag = release.get("tag_name")
                    version = ReleaseVersion.from_tag(tag) if isinstance(tag, str) else None
                    if version is None or version <= current:
                        continue
                    if channel == UpdateChannel.STABLE and version.is_beta:
                        continue
                    if release.get("prerelease") is not version.is_beta:
                        continue
                    assets = release.get("assets")
                    if not isinstance(assets, list) or not any(
                        isinstance(a, dict) and a.get("name") == "release-manifest.json"
                        for a in assets
                    ):
                        continue
                    try:
                        manifest = ReleaseManifest.parse(
                            self._get(release_asset_url(str(tag), MANIFEST_FILENAME))
                        )
                    except HttpStatusError as exc:
                        if exc.status_code in (403, 429):
                            raise
                        continue
                    except (ValueError, HttpError):
                        self._check()
                        continue
                    if manifest is not None and manifest.tag == tag and manifest.version == version:
                        published = {a.get("name"): a for a in assets if isinstance(a, dict)}
                        if all(
                            a.filename in published and published[a.filename].get("size") == a.size
                            for a in manifest.assets
                        ):
                            result.append(manifest)
                if len(releases) < 100:
                    break
        finally:
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
                temporary = self._directory / "github.json.tmp"
                temporary.write_text(json.dumps(self._cache), encoding="utf-8")
                temporary.replace(self._directory / "github.json")
            except OSError:
                pass  # A read-only cache must not prevent discovery.
        return result
