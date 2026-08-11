from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from solin.core.scenes.model import SceneValidationError


SCENE_COLLECTION_CATALOG_SCHEMA_VERSION = 1
DEFAULT_SCENE_COLLECTION_ID = "solin.scene-profile.default"
MAX_SCENE_COLLECTIONS = 64

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SceneCollectionError(ValueError):
    """Base error for scene-profile catalog operations."""


class UnsupportedSceneCollectionSchemaError(SceneCollectionError):
    def __init__(self, version: int) -> None:
        super().__init__(f"Unsupported scene-profile catalog schema version: {version}")
        self.version = version


@dataclass(frozen=True, slots=True)
class SceneCollection:
    """One user-facing Scene profile inside a general Solin profile."""

    id: str
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTITY_PATTERN.fullmatch(self.id):
            raise SceneCollectionError("Invalid Scene profile id")
        _validate_collection_name(self.name)

    def to_record(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_record(cls, raw: object) -> SceneCollection:
        data = _strict_mapping(raw, field_name="Scene profile", allowed_keys={"id", "name"})
        collection_id = data.get("id")
        name = data.get("name")
        if not isinstance(collection_id, str) or not isinstance(name, str):
            raise SceneCollectionError("Invalid Scene profile record")
        return cls(id=collection_id, name=name)


@dataclass(frozen=True, slots=True)
class SceneCollectionCatalog:
    revision: int
    active_collection_id: str
    collections: tuple[SceneCollection, ...]
    pending_collection_id: str = ""
    schema_version: int = SCENE_COLLECTION_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCENE_COLLECTION_CATALOG_SCHEMA_VERSION:
            raise UnsupportedSceneCollectionSchemaError(self.schema_version)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise SceneCollectionError("Invalid Scene profile catalog revision")
        if not isinstance(self.collections, tuple) or not all(
            isinstance(collection, SceneCollection) for collection in self.collections
        ):
            raise SceneCollectionError("Scene profiles must be an immutable typed tuple")
        if not self.collections:
            raise SceneCollectionError("At least one Scene profile is required")
        if len(self.collections) > MAX_SCENE_COLLECTIONS:
            raise SceneCollectionError("Too many Scene profiles")
        ids = tuple(collection.id for collection in self.collections)
        if len(ids) != len(set(ids)):
            raise SceneCollectionError("Duplicate Scene profile id")
        names = tuple(_normalized_name(collection.name) for collection in self.collections)
        if len(names) != len(set(names)):
            raise SceneCollectionError("Duplicate Scene profile name")
        if self.active_collection_id not in ids:
            raise SceneCollectionError("Active Scene profile does not exist")
        if self.pending_collection_id and self.pending_collection_id not in ids:
            raise SceneCollectionError("Pending Scene profile does not exist")

    @property
    def active(self) -> SceneCollection:
        return self.collection(self.active_collection_id)

    def collection(self, collection_id: str) -> SceneCollection:
        try:
            return next(item for item in self.collections if item.id == collection_id)
        except StopIteration as exc:
            raise SceneCollectionError("Scene profile does not exist") from exc

    def with_revision(self, revision: int) -> SceneCollectionCatalog:
        return replace(self, revision=revision)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "active_collection_id": self.active_collection_id,
            "pending_collection_id": self.pending_collection_id,
            "collections": [collection.to_record() for collection in self.collections],
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneCollectionCatalog:
        data = _strict_mapping(
            raw,
            field_name="Scene profile catalog",
            allowed_keys={
                "schema_version",
                "revision",
                "active_collection_id",
                "pending_collection_id",
                "collections",
            },
        )
        schema_version = data.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise SceneCollectionError("Invalid Scene profile catalog schema version")
        if schema_version != SCENE_COLLECTION_CATALOG_SCHEMA_VERSION:
            raise UnsupportedSceneCollectionSchemaError(schema_version)
        revision = data.get("revision")
        active_collection_id = data.get("active_collection_id")
        pending_collection_id = data.get("pending_collection_id", "")
        collections = data.get("collections")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(active_collection_id, str)
            or not isinstance(pending_collection_id, str)
            or not isinstance(collections, list)
        ):
            raise SceneCollectionError("Invalid Scene profile catalog record")
        return cls(
            schema_version=schema_version,
            revision=revision,
            active_collection_id=active_collection_id,
            pending_collection_id=pending_collection_id,
            collections=tuple(SceneCollection.from_record(item) for item in collections),
        )


def validate_scene_collection_name(
    name: str,
    *,
    existing: tuple[SceneCollection, ...] = (),
    excluding_id: str = "",
) -> str:
    _validate_collection_name(name)
    canonical = name.strip()
    normalized = _normalized_name(canonical)
    if any(
        collection.id != excluding_id and _normalized_name(collection.name) == normalized
        for collection in existing
    ):
        raise SceneCollectionError("A Scene profile with this name already exists")
    return canonical


def _validate_collection_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SceneCollectionError("Invalid Scene profile name")


def _normalized_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _strict_mapping(
    raw: object,
    *,
    field_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SceneCollectionError(f"{field_name} must be an object")
    unknown = set(raw) - allowed_keys
    if unknown:
        raise SceneValidationError(f"Unknown {field_name} fields: {', '.join(sorted(unknown))}")
    return dict(raw)
