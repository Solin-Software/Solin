"""Canonical identities for filesystem resources shared by background work."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """Shared/exclusive claim acquired atomically by background filesystem work."""

    exclusive_key: str = ""
    shared_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.exclusive_key and not self.shared_keys:
            raise ValueError("A resource claim must contain at least one key")
        if any(not key for key in self.shared_keys):
            raise ValueError("Shared resource keys must not be empty")
        normalized_shared = tuple(sorted(set(self.shared_keys)))
        if self.exclusive_key in normalized_shared:
            raise ValueError("A resource key cannot be both shared and exclusive")
        object.__setattr__(self, "shared_keys", normalized_shared)


def folder_resource_key(path: str | os.PathLike[str]) -> str:
    normalized = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
    return f"folder:{normalized}"


def file_resource_key(path: str | os.PathLike[str]) -> str:
    normalized = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
    return f"file:{normalized}"


def parent_folder_resource_key(path: str | os.PathLike[str]) -> str:
    return folder_resource_key(Path(path).parent)


def child_folder_resource_claim(path: str | os.PathLike[str]) -> ResourceClaim:
    """Claim one child exclusively while keeping its root available to siblings."""

    return ResourceClaim(
        exclusive_key=folder_resource_key(path),
        shared_keys=(parent_folder_resource_key(path),),
    )


def folder_read_resource_claim(path: str | os.PathLike[str]) -> ResourceClaim:
    """Claim a folder for reading while excluding root-wide mutations."""

    return ResourceClaim(shared_keys=(folder_resource_key(path),))


__all__ = [
    "ResourceClaim",
    "child_folder_resource_claim",
    "file_resource_key",
    "folder_read_resource_claim",
    "folder_resource_key",
    "parent_folder_resource_key",
]
