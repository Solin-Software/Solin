"""Global ownership, legacy publication migration, and startup asset collection."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, IO
import zipfile

from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.core.ingest.sync.journal import JournalReplica, ReplicaSnapshot, _validate
from solin.core.media.formats import media_type_from_path
from solin.core.media.publication_assets import PublicationAssetStore
from solin.core.meetings.content_policy import MeetingContentPolicy
from solin.core.meetings.jwpub_cache import JwpubCache
from solin.core.meetings.meeting_weeks import current_monday, meeting_week_bounds
from solin.core.meetings.tree_builder import canonical_media_source_hash
from solin.core.meetings.tree_store import MeetingTreeStore, parse_meeting_tree_key
from solin.core.meetings.tree_types import source_hash
from solin.core.playlists.cleanup import (
    build_profile_cleanup_references,
    collect_media_paths,
    iter_media_path_fields,
    iter_meeting_nodes_strict,
    load_profile_scene_references,
)
from solin.core.playlists.storage import PLAYLIST_STORE_VERSION, PlaylistRepository
from solin.core.storage.json_repository import JsonFileRepository


log = logging.getLogger(__name__)


@dataclass(slots=True)
class _ProfileOwners:
    paths: ProfilePaths
    meetings: dict[str, Any]
    playlists: list[dict]
    scene_references: set[str]


class MeetingAssetLifecycle:
    """Run before profile maintenance and before mutable runtime owners exist.

    Validation spans every on-disk profile, including unregistered directories.
    Errors propagate before object collection. On failure the publication cache
    root is protected: callers must treat a protected directory as protecting
    all its descendants. Publication cache pruning follows successful reference
    validation and runs after the asset store releases its collection lock.
    """

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        asset_store: PublicationAssetStore,
        policy: MeetingContentPolicy,
        *,
        profile_catalog: Callable[[], Iterable[str | ProfilePaths]] | None = None,
        active_paths_provider: Callable[[], set[str]] | None = None,
        today: date | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._assets = asset_store
        self._policy = policy
        self._profile_catalog = profile_catalog
        self._active_paths_provider = active_paths_provider or set
        self._today = today
        self._protected: set[str] = {str(runtime_paths.jwpub_cache_dir)}
        self._preferred_publications: set[tuple[str, str, str]] = set()
        self._provenance_cache: dict[str, dict[str, str] | None] = {}
        self._archive_checksums: dict[Path, str] = {}
        self._translations: dict[str, str] = {}
        self._workers = ThreadedWorkerPool()
        self._request_lock = threading.Lock()
        self._collection_requested = False
        self._collection_running = False
        self._closed = False
        self._owners_lock = threading.RLock()
        self._profile_owners: dict[str, int] = {}
        self._runtime_ownership_registered = False

    def protected_publication_sources(self) -> set[str]:
        """Sources still required by unresolved references or immutable journals."""
        return set(self._protected)

    def maintain(self) -> list[str]:
        """Validate all owners, prune expired weeks, migrate, then collect objects."""
        with self._owners_lock:
            if self._profile_owners:
                self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
                raise RuntimeError("Startup asset maintenance requires closed profile owners")
            return self._maintain_startup()

    def _maintain_startup(self) -> list[str]:
        self._protected = {str(self._runtime_paths.jwpub_cache_dir)}
        self._provenance_cache.clear()
        self._archive_checksums.clear()
        self._translations.clear()
        try:
            profiles = self._load_profiles()
            # Validate pending intentions before changing even an inactive profile.
            self._journal_references(profiles)
            cutoff, _ = meeting_week_bounds(self._today)
            for profile in profiles:
                store = MeetingTreeStore(profile.paths.meeting_trees_file)
                store.prune_before(cutoff)
                profile.meetings = store.load_all_strict()
            for profile in profiles:
                self._migrate_profile(profile)
            self._protected = set()
            return self.collect()
        except Exception:  # noqa: BLE001 - fail closed across repository adapters
            self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
            raise

    def register_profile_owner(self, profile_id: str) -> Callable[[], None]:
        """Register before loading repositories; release only after closing them.

        Cached profiles also need registration. The returned release callback is
        idempotent. Registration serializes with inactive expiry so a repository
        can never load a soon-to-be-overwritten cached snapshot.
        """
        from solin.core.profiles.models import validate_profile_id

        profile_id = validate_profile_id(profile_id)
        with self._owners_lock:
            if self._closed:
                raise RuntimeError("Meeting asset lifecycle is closed")
            self._runtime_ownership_registered = True
            self._profile_owners[profile_id] = self._profile_owners.get(profile_id, 0) + 1
        released = False

        def release() -> None:
            nonlocal released
            with self._owners_lock:
                if released:
                    return
                released = True
                remaining = self._profile_owners[profile_id] - 1
                if remaining:
                    self._profile_owners[profile_id] = remaining
                else:
                    del self._profile_owners[profile_id]

        return release

    def prune_inactive(self) -> int:
        """Expire unowned profiles only after runtime registration is integrated."""
        with self._owners_lock:
            if not self._runtime_ownership_registered or self._closed:
                return 0
            try:
                profiles = self._load_profiles()
                self._journal_references(profiles)
                cutoff, _ = meeting_week_bounds(self._today)
                return sum(
                    MeetingTreeStore(profile.paths.meeting_trees_file).prune_before(cutoff)
                    for profile in profiles
                    if profile.paths.profile_dir.name not in self._profile_owners
                )
            except Exception:  # noqa: BLE001 - inactive repository boundary
                self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
                raise

    def request_collection(self) -> None:
        """Coalesce runtime requests into one worker, replaying concurrent commits."""
        with self._request_lock:
            if self._closed:
                return
            self._collection_requested = True
            if self._collection_running:
                return
            self._collection_running = True
            try:
                handle = self._workers.submit("meeting-asset-collection", self._run_collection)
            except Exception:  # noqa: BLE001 - best-effort scheduling boundary
                self._collection_running = False
                self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
                log.warning("Could not schedule meeting asset collection", exc_info=True)
                return
            if handle is None:
                self._collection_running = False

    def _run_collection(self) -> None:
        while True:
            with self._request_lock:
                if self._closed or not self._collection_requested:
                    self._collection_running = False
                    return
                self._collection_requested = False
            try:
                self.prune_inactive()
                self.collect()
            except Exception:  # noqa: BLE001 - best-effort maintenance boundary
                self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
                log.warning(
                    "Meeting asset collection failed; publication cache protected", exc_info=True
                )

    def shutdown(self) -> None:
        """Reject new work, discard queued reruns, and drain the owned worker."""
        with self._request_lock:
            self._closed = True
            self._collection_requested = False
        alive = self._workers.shutdown()
        if alive:
            log.warning("Meeting asset collection is still stopping: %s", ", ".join(alive))

    def collect(self) -> list[str]:
        """Revalidate owners, collect assets, then prune the publication cache.

        Runtime callers must retain/lease unsaved assets in the shared store or
        expose them through active_paths_provider. Repository writes and expiry
        belong to their existing owners after startup. Both injected providers
        run on the collection worker and must be safe to call off the GUI thread.
        """
        try:
            removed = self._assets.collect(
                self._references, retain_images=self._policy.keep_images_for_manual_cleanup
            )
            # Workers acquire the archive lock before publishing into the asset
            # store. Never invert that ordering by pruning under collect's lock.
            JwpubCache(self._runtime_paths.jwpub_cache_dir).prune(
                protected_sources=self.protected_publication_sources(),
                preferred_publications=self._preferred_publications,
            )
            return removed
        except Exception:  # noqa: BLE001 - protect unresolved repository adapters
            self._protected.add(str(self._runtime_paths.jwpub_cache_dir))
            raise

    def _profile_paths(self) -> list[ProfilePaths]:
        runtime = self._runtime_paths
        root = runtime.data_dir / "profiles"
        profiles: dict[str, ProfilePaths] = {}
        if root.exists():
            for child in sorted(root.iterdir()):
                if child.is_symlink() or child.is_junction():
                    raise ValueError("Profile owners cannot be filesystem links")
                if child.is_dir():
                    profiles[child.name] = ProfilePaths.from_roots(
                        data_dir=runtime.data_dir,
                        cache_dir=runtime.cache_dir,
                        profile_id=child.name,
                    )
        if self._profile_catalog is None:
            from solin.core.profiles.infrastructure import JsonProfileRegistry

            entries = (
                profile.id
                for profile in JsonProfileRegistry(runtime.data_dir / "profiles.json").load()
            )
        else:
            entries = self._profile_catalog()
        for entry in entries:
            if isinstance(entry, ProfilePaths):
                paths = entry
                profile_id = paths.profile_dir.name
            elif isinstance(entry, str) and entry and Path(entry).name == entry:
                profile_id = entry
                paths = ProfilePaths.from_roots(
                    data_dir=runtime.data_dir,
                    cache_dir=runtime.cache_dir,
                    profile_id=profile_id,
                )
            else:
                raise ValueError("Invalid profile catalog owner")
            if paths.profile_dir.resolve() != (root / profile_id).absolute():
                raise ValueError("Profile owner is outside the profile root")
            if not paths.profile_dir.is_dir():
                raise FileNotFoundError(paths.profile_dir)
            profiles[profile_id] = paths
        return list(profiles.values())

    def _load_profiles(self) -> list[_ProfileOwners]:
        result = []
        for paths in self._profile_paths():
            meetings = MeetingTreeStore(paths.meeting_trees_file).load_all_strict()
            playlists = PlaylistRepository(paths.playlists_file).load_strict()
            if paths.playlists_file.exists():
                raw = JsonFileRepository(paths.playlists_file).read()
                version = raw.get("version", 0)
                if (
                    isinstance(version, bool)
                    or not isinstance(version, int)
                    or version > PLAYLIST_STORE_VERSION
                    or version < 0
                ):
                    raise ValueError("Unsupported playlist owner schema")
            build_profile_cleanup_references(playlists, meetings)
            self._validate_linked_snapshots(meetings, playlists)
            scenes = load_profile_scene_references(paths)
            scene_asset_ids = {
                Path(identifier).stem
                for identifier in scenes.asset_ids
                if re.fullmatch(r"[0-9a-f]{64}", Path(identifier).stem)
            }
            result.append(
                _ProfileOwners(
                    paths, meetings, playlists, set(scenes.media_paths) | scene_asset_ids
                )
            )
        return result

    @staticmethod
    def _validate_linked_snapshots(meetings: dict, playlists: list[dict]) -> None:
        for record in meetings["trees"].values():
            binding = record.get("linked_sync", {})
            if not isinstance(binding, dict):
                raise ValueError("Invalid meeting linked sync binding")
            if "snapshot" in binding:
                ReplicaSnapshot.from_dict(binding["snapshot"])
        for playlist in playlists:
            if "__linked_sync" in playlist:
                ReplicaSnapshot.from_dict(playlist["__linked_sync"])

    def _journal_references(
        self, profiles: list[_ProfileOwners], *, protected: set[str] | None = None
    ) -> set[str]:
        protected = self._protected if protected is None else protected
        references: set[str] = set()
        state_root = self._runtime_paths.data_dir / "linked_sync"
        bindings: set[tuple[str, str]] = set()
        for profile in profiles:
            for record in profile.meetings["trees"].values():
                binding = record.get("linked_sync", {})
                folder = binding.get("folder")
                if folder:
                    if not isinstance(folder, str) or not os.path.isabs(folder):
                        raise ValueError("Invalid linked meeting folder")
                    bindings.add((folder, "meeting"))
        for folder, namespace in bindings:
            replica = JournalReplica(Path(folder), namespace, state_dir=state_root)
            snapshot = replica.read_local()
            references.update(collect_media_paths(snapshot.entities))
        if state_root.exists():
            for path in sorted(state_root.rglob("*.json")):
                if path.parent.name == "intents":
                    raw = JsonFileRepository(path).read()
                    build_profile_cleanup_references([raw], {"trees": {}})
                    references.update(collect_media_paths(raw))
                    continue
                if path.parent.name not in {"operations", "outbox"}:
                    continue
                raw = JsonFileRepository(path).read()
                if not isinstance(raw, dict) or raw.get("namespace") not in {"meeting", "playlist"}:
                    raise ValueError("Invalid linked journal owner")
                operation = _validate(raw, raw["namespace"])
                sources = collect_media_paths(operation)
                # Immutable historical operations can reconstruct raw extraction
                # paths. Pin those sources, without retaining deleted objects.
                self._protect_sources(sources, protected)
                if path.parent.name == "outbox":
                    references.update(sources)
        self._protect_sources(references, protected)
        return references

    def _references(self) -> set[str]:
        profiles = self._load_profiles()
        protected: set[str] = set()
        preferred: set[tuple[str, str, str]] = set()
        anchor = current_monday(self._today)
        preferred_weeks = {anchor, anchor + timedelta(weeks=1)}
        references = self._journal_references(profiles, protected=protected)
        for profile in profiles:
            references.update(collect_media_paths(profile.meetings))
            references.update(collect_media_paths(profile.playlists))
            references.update(profile.scene_references)
            for key, record in profile.meetings["trees"].items():
                parsed = parse_meeting_tree_key(key)
                if parsed is None or parsed.monday not in preferred_weeks:
                    continue
                if parsed.pub_type in {"mwb", "wt"}:
                    preferred.add(
                        ("w" if parsed.pub_type == "wt" else "mwb", parsed.language, parsed.issue)
                    )
                preferred.update(
                    (dependency["pub"], dependency["language"], dependency["issue"])
                    for dependency in record.get("publication_dependencies", [])
                )
        active = self._active_paths_provider()
        if not isinstance(active, set) or any(not isinstance(path, str) for path in active):
            raise ValueError("Active asset references must be a set of strings")
        references.update(active)
        self._protect_sources(references, protected)
        self._protected = protected
        self._preferred_publications = preferred
        return references

    def _protect_sources(self, sources: Iterable[str], protected: set[str]) -> None:
        for source in sources:
            if self._is_publication_path(source):
                protected.add(source)

    def _is_publication_path(self, source: str) -> bool:
        if not os.path.isabs(source):
            return False
        return Path(source).resolve().is_relative_to(self._runtime_paths.jwpub_cache_dir.resolve())

    def _migrate_profile(self, profile: _ProfileOwners) -> None:
        meetings = copy.deepcopy(profile.meetings)
        playlists = copy.deepcopy(profile.playlists)
        for _tree_key, node in iter_meeting_nodes_strict(meetings):
            self._migrate_media(node)
        for key, record in meetings["trees"].items():
            # linked_sync.snapshot is the immutable causal baseline. Its original
            # paths stay pinned; only local materialized views are translated.
            if record != profile.meetings["trees"][key] and record.get("canonical_nodes"):
                record["last_canonical_hash"] = source_hash(record["canonical_nodes"])
        for playlist in playlists:
            for item in playlist["items"]:
                self._migrate_media(item)
        meetings = self._translate_paths(meetings)
        playlists = self._translate_paths(playlists)
        if meetings != profile.meetings:
            JsonFileRepository(profile.paths.meeting_trees_file).write(meetings)
        if playlists != profile.playlists:
            PlaylistRepository(profile.paths.playlists_file).save_strict(playlists)

    def _migrate_media(self, node: dict) -> None:
        ref = node.get("media_ref")
        primary = next(
            (
                value
                for value in (
                    str((ref or {}).get("file_path") or ""),
                    str(node.get("url") or ""),
                    str(node.get("resolved_url") or ""),
                )
                if value
                and self._is_publication_path(value)
                and media_type_from_path(value) == "image"
            ),
            "",
        )
        provenance = (
            self._provenance(primary) if primary and self._is_publication_path(primary) else None
        )
        changed = False
        local = {key: value for key, value in node.items() if key != "children"}
        for owner, field in iter_media_path_fields(local):
            source = owner[field]
            if not self._is_publication_path(source):
                continue
            if media_type_from_path(source) != "image":
                self._protected.add(source)
                continue
            if self._provenance(source) is None:
                self._protected.add(source)
                continue
            destination = self._translations.get(source)
            if destination is None:
                destination = self._assets.materialize(source, title=str(node.get("title") or ""))
                self._translations[source] = destination
            owner[field] = destination
            changed = True
        node.update(local)
        if changed and provenance is not None:
            metadata = ref if isinstance(ref, dict) else node
            metadata["publication_source"] = provenance
            destination = self._translations[primary]
            metadata["asset_hash"] = self._assets.asset_id(destination)
            if node.get("meeting_source_key"):
                # Keys and occurrence IDs are durable: moving bytes must not
                # orphan framing, deletion tombstones, or hidden canonical maps.
                node["meeting_source_hash"] = canonical_media_source_hash(node)

    def _translate_paths(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                self._translations.get(key, key): (
                    child
                    if key in {"publication_source", "linked_sync", "__linked_sync"}
                    else self._translate_paths(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self._translate_paths(child) for child in value]
        if isinstance(value, str):
            return self._translations.get(value, value)
        return value

    def _provenance(self, source: str) -> dict[str, str] | None:
        if source not in self._provenance_cache:
            self._provenance_cache[source] = self._read_provenance(source)
        return self._provenance_cache[source]

    def _read_provenance(self, source: str) -> dict[str, str] | None:
        relative = Path(source).resolve().relative_to(self._runtime_paths.jwpub_cache_dir.resolve())
        if len(relative.parts) < 3 or not relative.parts[1].startswith("x_"):
            return None
        publication, _, language = relative.parts[0].rpartition("_")
        issue = relative.parts[1][2:]
        if not publication or not language or not issue:
            return None
        archive = (
            self._runtime_paths.jwpub_cache_dir
            / relative.parts[0]
            / f"{relative.parts[0]}_{issue}.jwpub"
        )
        # A checksum describes the archive which supplied these bytes. The
        # remote checksum catalog alone cannot prove an old extraction's origin.
        if not archive.is_file():
            return None
        member = Path(*relative.parts[2:]).as_posix()
        # A newer archive can coexist with an older extraction. Do not assign
        # that newer archive's identity to bytes it never contained.
        try:
            with Path(source).open("rb") as stream:
                source_digest = _sha256(stream)
            with zipfile.ZipFile(archive) as outer:
                with outer.open("contents") as contents, zipfile.ZipFile(contents) as inner:
                    with inner.open(member) as stream:
                        if _sha256(stream) != source_digest:
                            return None
        except (FileNotFoundError, KeyError, zipfile.BadZipFile):
            return None
        if archive not in self._archive_checksums:
            with archive.open("rb") as stream:
                digest = hashlib.md5(usedforsecurity=False)
                self._archive_checksums[archive] = hashlib.file_digest(
                    stream, lambda: digest
                ).hexdigest()
        return {
            "pub": publication,
            "language": language,
            "issue": issue,
            "checksum": self._archive_checksums[archive],
            "member": member,
        }


def _sha256(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
    return digest.hexdigest()
