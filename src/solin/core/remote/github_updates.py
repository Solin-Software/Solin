"""Bounded GitHub release queries with shared, atomic conditional caching."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, RLock
from weakref import WeakValueDictionary

from solin.core.network.http import (
    HttpDecodeError,
    HttpError,
    HttpResponseTooLargeError,
    HttpStatusError,
    stream_get,
)
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    MAX_MANIFEST_BYTES,
    MANIFEST_FILENAME,
    RELEASE_REPOSITORY,
    ReleaseManifest,
    release_asset_url,
)
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.release_history import ReleaseHistoryCursorError, ReleaseHistoryPage
from solin.core.remote.update_policy import ReleaseNote, is_update_candidate
from solin.core.storage.json_files import write_json_atomic

_LOGGER = logging.getLogger(__name__)
_MAX_LIST_PAGES = 10
_MAX_HISTORY_SNAPSHOTS = 8
_MAX_RECENT_CACHES = 8
_CACHES_LOCK = Lock()


class _Cache:
    """Clients for one directory serialize queries, including HTTP and persistence."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "github.json"
        self.lock = RLock()
        self.data: dict = {}
        self.loaded = False
        self.signature: tuple[int, int, int, int] | None = None

    def file_signature(self) -> tuple[int, int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def load(self) -> None:
        try:
            signature = self.file_signature()
        except OSError:
            _LOGGER.debug("GitHub cache could not be inspected", exc_info=True)
            return
        if self.loaded and signature == self.signature:
            return
        self.data = {}
        if signature is not None:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self.data = payload
            except (OSError, ValueError):
                _LOGGER.debug("GitHub cache unavailable", exc_info=True)
        self.signature = signature
        self.loaded = True


_CACHES: WeakValueDictionary[Path, _Cache] = WeakValueDictionary()
# Keep pagination usable when disk persistence is unavailable and consumers
# construct a client per page. Weak entries still coordinate evicted active clients.
_RECENT_CACHES: OrderedDict[Path, _Cache] = OrderedDict()


def _shared_cache(directory: Path) -> _Cache:
    directory = directory.resolve()
    with _CACHES_LOCK:
        cache = _CACHES.get(directory)
        if cache is None:
            cache = _Cache(directory)
            _CACHES[directory] = cache
        _RECENT_CACHES.pop(directory, None)
        _RECENT_CACHES[directory] = cache
        while len(_RECENT_CACHES) > _MAX_RECENT_CACHES:
            _RECENT_CACHES.popitem(last=False)
        return cache


@dataclass(frozen=True, slots=True)
class _PublishedRelease:
    version: ReleaseVersion
    assets: tuple[tuple[str, int], ...]

    @classmethod
    def parse(cls, release: object) -> _PublishedRelease | None:
        if not isinstance(release, dict) or release.get("draft") is not False:
            return None
        tag = release.get("tag_name")
        version = ReleaseVersion.from_tag(tag) if isinstance(tag, str) else None
        if version is None or release.get("prerelease") is not version.is_beta:
            return None
        assets = release.get("assets")
        if not isinstance(assets, list):
            return None
        inventory = tuple(
            (asset["name"], asset["size"])
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and type(asset.get("size")) is int
        )
        if MANIFEST_FILENAME not in dict(inventory):
            return None
        if len(dict(inventory)) != len(inventory):
            return None
        return cls(version, inventory)

    def to_dict(self) -> dict:
        return {
            "draft": False,
            "prerelease": self.version.is_beta,
            "tag_name": self.version.tag,
            "assets": [{"name": name, "size": size} for name, size in self.assets],
        }


class _Query:
    def __init__(self, cache: _Cache, cancelled: Event) -> None:
        self.cache = cache
        self.cancelled = cancelled
        self.deadline = time.monotonic() + 20.0
        self.dirty = False

    def check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise HttpError("Release query cancelled or timed out")
        return remaining

    def backed_off(self) -> bool:
        retry_after = self.cache.data.get("retry_after", 0)
        return isinstance(retry_after, (float, int)) and retry_after > time.time()

    def get(self, url: str, *, immutable: bool = False) -> object:
        remaining = self.check()
        entry = self.cache.data.get(url, {})
        if not isinstance(entry, dict):
            entry = {}
        if immutable and entry.get("validated") is True and "payload" in entry:
            return entry["payload"]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Solin-update-check"}
        if isinstance(entry.get("etag"), str) and entry["etag"]:
            headers["If-None-Match"] = entry["etag"]
        try:
            with stream_get(url, timeout=min(5.0, remaining), headers=headers) as response:
                self.check()
                if response.status_code == 304:
                    if "payload" not in entry:
                        raise HttpError("Conditional response without cached release")
                    return entry["payload"]
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    self.check()
                    chunks.extend(chunk)
                    if len(chunks) > MAX_MANIFEST_BYTES:
                        raise HttpResponseTooLargeError(url, MAX_MANIFEST_BYTES)
                self.check()
                try:
                    payload = json.loads(chunks)
                except (ValueError, UnicodeError) as exc:
                    raise HttpDecodeError("Invalid release metadata JSON") from exc
                self.cache.data[url] = {
                    "etag": response.headers.get("ETag", ""), "payload": payload,
                }
                self.dirty = True
                return payload
        except HttpStatusError as exc:
            if exc.status_code in (403, 429):
                self.cache.data["retry_after"] = time.time() + 3600
                self.dirty = True
            raise

    def releases(self) -> list[_PublishedRelease]:
        result: dict[ReleaseVersion, _PublishedRelease] = {}
        for page in range(1, _MAX_LIST_PAGES + 1):
            releases = self.get(
                f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases?per_page=100&page={page}"
            )
            if not isinstance(releases, list):
                raise HttpDecodeError("Invalid GitHub releases response")
            for raw in releases:
                release = _PublishedRelease.parse(raw)
                if release is not None:
                    result.setdefault(release.version, release)
            if len(releases) < 100:
                break
        return sorted(result.values(), key=lambda release: release.version, reverse=True)

    def manifest(self, release: _PublishedRelease) -> ReleaseManifest | None:
        url = release_asset_url(release.version.tag, MANIFEST_FILENAME)
        try:
            manifest = ReleaseManifest.parse(self.get(url, immutable=True))
        except HttpStatusError as exc:
            if exc.status_code not in (404, 410):
                raise
            return None
        except (HttpDecodeError, HttpResponseTooLargeError):
            self.check()
            return None
        if manifest is None or manifest.version != release.version:
            return None
        inventory = dict(release.assets)
        if any(inventory.get(asset.filename) != asset.size for asset in manifest.assets):
            return None
        entry = self.cache.data[url]
        if entry.get("validated") is not True:
            entry["validated"] = True
            self.dirty = True
        return manifest

    def snapshot(self, releases: list[_PublishedRelease]) -> str:
        payload = [release.to_dict() for release in releases]
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        snapshots = self.cache.data.get("history_snapshots")
        if not isinstance(snapshots, dict):
            snapshots = {}
            self.cache.data["history_snapshots"] = snapshots
        # Refresh insertion order so recently opened queries remain resumable.
        snapshots.pop(identity, None)
        snapshots[identity] = payload
        while len(snapshots) > _MAX_HISTORY_SNAPSHOTS:
            del snapshots[next(iter(snapshots))]
        self.dirty = True
        return identity


def _encode_cursor(identity: str, offset: int, include_beta: bool, language: str) -> str:
    payload = json.dumps([1, identity, offset, include_beta, language]).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_cursor(cursor: str, include_beta: bool, language: str) -> tuple[str, int]:
    try:
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ReleaseHistoryCursorError("Invalid release-history cursor") from exc
    if (
        not isinstance(payload, list) or len(payload) != 5
        or type(payload[0]) is not int or payload[0] != 1
        or not isinstance(payload[1], str) or len(payload[1]) != 64
        or type(payload[2]) is not int or payload[2] < 0
        or type(payload[3]) is not bool or payload[3] != include_beta
        or payload[4] != language
    ):
        raise ReleaseHistoryCursorError("Invalid or mismatched release-history cursor")
    return payload[1], payload[2]


class GitHubReleases:
    def __init__(self, cache_directory: Path, cancelled: Event | None = None) -> None:
        self._cache = _shared_cache(cache_directory)
        self._cancelled = cancelled or Event()

    @contextmanager
    def _query(self) -> Iterator[_Query]:
        query = _Query(self._cache, self._cancelled)
        while not self._cache.lock.acquire(timeout=min(0.1, query.check())):
            query.check()
        try:
            query.check()
            self._cache.load()
            yield query
        finally:
            try:
                if query.dirty:
                    write_json_atomic(self._cache.path, self._cache.data)
                    self._cache.signature = self._cache.file_signature()
            except OSError:
                _LOGGER.debug("GitHub cache could not be persisted", exc_info=True)
            finally:
                self._cache.lock.release()

    def discover(self, current_version: str, channel: UpdateChannel) -> list[ReleaseManifest]:
        current = ReleaseVersion.parse(current_version)
        if current is None:
            return []
        with self._query() as query:
            if query.backed_off():
                return []
            result = []
            for release in query.releases():
                if not is_update_candidate(release.version, current_version=current, channel=channel):
                    continue
                # Validated manifests share the complete package inventory and
                # OS floors. Once a target exists, older versions cannot improve
                # compatibility or contribute to an upgrade's incremental notes.
                if release.version < current and result:
                    break
                try:
                    manifest = query.manifest(release)
                except HttpStatusError as exc:
                    if exc.status_code in (403, 429):
                        raise
                    continue
                except HttpError:
                    query.check()
                    continue
                if manifest is not None:
                    result.append(manifest)
            return result

    def history(
        self,
        *,
        include_beta: bool = False,
        language: str = "en",
        cursor: str | None = None,
        page_size: int = 5,
    ) -> ReleaseHistoryPage:
        """Fetch own-version notes, independently of installed version or platform.

        A new query (cursor=None) revalidates the release lists. Continuations
        reuse a persistent snapshot and validated manifests, even with a new client.
        The last eight distinct list snapshots are retained; expired/mismatched
        cursors raise ReleaseHistoryCursorError. HTTP errors, backoff and cancellation raise
        HttpError so consumers can retry without discarding already displayed pages.
        Calls are blocking and belong on a worker, never the GUI thread.
        """
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Release-history page size must be between 1 and 100")
        continuation = _decode_cursor(cursor, include_beta, language) if cursor is not None else None
        with self._query() as query:
            if query.backed_off():
                raise HttpError("GitHub release queries are temporarily rate limited")
            if continuation is None:
                releases = query.releases()
                identity, offset = query.snapshot(releases), 0
            else:
                identity, offset = continuation
                snapshots = self._cache.data.get("history_snapshots", {})
                payload = snapshots.get(identity) if isinstance(snapshots, dict) else None
                if not isinstance(payload, list):
                    raise ReleaseHistoryCursorError("Release-history cursor expired; start a new query")
                releases = []
                for raw in payload:
                    release = _PublishedRelease.parse(raw)
                    if release is None:
                        raise ReleaseHistoryCursorError("Invalid cached release-history snapshot")
                    releases.append(release)
            releases = [release for release in releases if include_beta or not release.version.is_beta]
            if offset > len(releases):
                raise ReleaseHistoryCursorError("Invalid release-history cursor offset")
            items: list[ReleaseNote] = []
            for index in range(offset, len(releases)):
                try:
                    manifest = query.manifest(releases[index])
                except HttpError as exc:
                    query.check()
                    if len(items) < page_size or (
                        isinstance(exc, HttpStatusError) and exc.status_code in (403, 429)
                    ):
                        raise
                    # The page is complete. Defer a transient lookahead failure
                    # to its continuation instead of discarding ready entries.
                    return ReleaseHistoryPage(
                        tuple(items), _encode_cursor(identity, index, include_beta, language)
                    )
                if manifest is None:
                    continue
                if len(items) == page_size:
                    return ReleaseHistoryPage(
                        tuple(items), _encode_cursor(identity, index, include_beta, language)
                    )
                note = next(note for note in manifest.notes if note.version == manifest.version)
                locale = language if language in note.translations else "en"
                items.append(ReleaseNote(note.version, note.translations[locale], locale))
            return ReleaseHistoryPage(tuple(items))
