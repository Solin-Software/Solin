from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import threading
from collections.abc import Callable

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.foundation.resource_keys import ResourceClaim, file_resource_key
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.storage.json_repository import JsonFileRepository


@dataclass(frozen=True, slots=True)
class PlaylistStoragePaths:
    playlists_file: Path
    pending_deletions_file: Path


class PlaylistRepository:
    """Repository for the profile-local ``playlists.json`` file."""

    def __init__(
        self,
        path: str | Path,
        *,
        resource_lanes: ResourceLaneRegistry | None = None,
    ) -> None:
        self._json = JsonFileRepository(path)
        self._resource_lanes = resource_lanes
        self._listener_lock = threading.RLock()
        self._listeners: set[Callable[[], None]] = set()

    @classmethod
    def from_paths(
        cls,
        paths: PlaylistStoragePaths,
        *,
        resource_lanes: ResourceLaneRegistry | None = None,
    ) -> "PlaylistRepository":
        return cls(paths.playlists_file, resource_lanes=resource_lanes)

    @property
    def path(self) -> Path:
        return self._json.path

    def load(self) -> list[dict]:
        try:
            if self._json.exists():
                data = self._json.read()
                return data.get("playlists", []) if isinstance(data, dict) else []
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            log_ignored_exception(__name__, "Could not load playlists file")
        return []

    def load_strict(self) -> list[dict]:
        """Load playlists while preserving read/parse failures for destructive callers."""
        def load() -> list[dict]:
            if not self._json.exists():
                return []
            data = self._json.read()
            if not isinstance(data, dict):
                raise ValueError("Playlist storage root must be an object")
            playlists = data.get("playlists", [])
            if not isinstance(playlists, list):
                raise ValueError("Playlist storage 'playlists' must be a list")
            return playlists

        return self._run_shared(load)

    def save(self, playlists: list[dict]) -> None:
        try:
            self.save_strict(playlists)
        except (OSError, UnicodeError, TypeError, ValueError):
            log_ignored_exception(__name__, "Could not save playlists file")

    def save_strict(self, playlists: list[dict]) -> None:
        """Atomically persist playlists and propagate serialization/write failures."""
        self._run_exclusive(lambda: self._json.write({"playlists": playlists}))
        self._publish_changed()

    def _run_shared(self, action):
        if self._resource_lanes is None:
            return action()
        return self._resource_lanes.run(
            ResourceClaim(shared_keys=(file_resource_key(self.path),)),
            action,
        )

    def _run_exclusive(self, action):
        if self._resource_lanes is None:
            return action()
        return self._resource_lanes.run(file_resource_key(self.path), action)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to successful atomic writes and return an unsubscribe callback."""
        with self._listener_lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._listener_lock:
                self._listeners.discard(listener)

        return unsubscribe

    def _publish_changed(self) -> None:
        with self._listener_lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 - repository observer boundary
                log_ignored_exception(__name__, "Playlist change listener failed")


class PendingDeletionRepository:
    """Repository for pending filesystem deletions that could not complete."""

    def __init__(
        self,
        path: str | Path,
        *,
        resource_lanes: ResourceLaneRegistry | None = None,
    ) -> None:
        self._json = JsonFileRepository(path)
        self._resource_lanes = resource_lanes

    @classmethod
    def from_paths(
        cls,
        paths: PlaylistStoragePaths,
        *,
        resource_lanes: ResourceLaneRegistry | None = None,
    ) -> "PendingDeletionRepository":
        return cls(paths.pending_deletions_file, resource_lanes=resource_lanes)

    @property
    def path(self) -> Path:
        return self._json.path

    def load(self) -> list[str]:
        try:
            return self.load_strict()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            log_ignored_exception(__name__, "Could not load pending deletions file")
        return []

    def load_strict(self) -> list[str]:
        """Load pending deletions while preserving invalid schema for cleanup callers."""
        def load() -> list[str]:
            if not self._json.exists():
                return []
            data = self._json.read()
            if not isinstance(data, dict):
                raise ValueError("Pending deletion storage root must be an object")
            pending = data.get("pending", [])
            if not isinstance(pending, list):
                raise ValueError("Pending deletion storage 'pending' must be a list")
            if not all(isinstance(path, str) for path in pending):
                raise ValueError("Pending deletion entries must be strings")
            return pending

        if self._resource_lanes is None:
            return load()
        return self._resource_lanes.run(
            ResourceClaim(shared_keys=(file_resource_key(self.path),)),
            load,
        )

    def save(self, media_paths: list[str]) -> None:
        try:
            self.save_strict(media_paths)
        except (OSError, UnicodeError, TypeError, ValueError):
            log_ignored_exception(__name__, "Could not save pending deletions file")

    def save_strict(self, media_paths: list[str]) -> None:
        if not all(isinstance(path, str) for path in media_paths):
            raise ValueError("Pending deletion entries must be strings")
        action = lambda: self._json.write({"pending": media_paths})
        if self._resource_lanes is None:
            action()
            return
        self._resource_lanes.run(file_resource_key(self.path), action)
