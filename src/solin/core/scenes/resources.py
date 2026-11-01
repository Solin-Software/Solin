from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from solin.core.scenes.model import (
    CameraPreset,
    LocalCameraConfig,
    RtspCameraConfig,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    camera_source_from_legacy_record,
)


SCENE_RESOURCE_SCHEMA_VERSION = 3
MAX_SHARED_CAMERAS = 128


class UnsupportedSceneResourceSchemaError(SceneValidationError):
    def __init__(self, version: int) -> None:
        super().__init__(f"Unsupported shared scene-resource schema version: {version}")
        self.version = version


def camera_credential_references(camera: SourceDefinition) -> tuple[str, ...]:
    """Protected stream and PTZ credentials owned by a connection."""
    references = [camera.credential_ref] if camera.credential_ref else []
    configuration = camera.configuration
    if isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
        reference = getattr(configuration.ptz_binding, "credential_ref", "")
        if reference:
            references.append(reference)
    return tuple(dict.fromkeys(references))


@dataclass(frozen=True, slots=True)
class SceneResourceCatalog:
    """Camera connections and PTZ presets shared by one general Solin profile."""

    revision: int
    cameras: tuple[SourceDefinition, ...] = ()
    camera_presets: tuple[CameraPreset, ...] = ()
    pending_credential_deletions: tuple[str, ...] = ()
    schema_version: int = SCENE_RESOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise SceneValidationError("Shared scene-resource schema version must be an integer")
        if self.schema_version != SCENE_RESOURCE_SCHEMA_VERSION:
            raise UnsupportedSceneResourceSchemaError(self.schema_version)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise SceneValidationError("Invalid shared scene-resource revision")
        if not isinstance(self.cameras, tuple) or not all(
            isinstance(camera, SourceDefinition) for camera in self.cameras
        ):
            raise SceneValidationError("Shared cameras must be an immutable typed tuple")
        if not isinstance(self.camera_presets, tuple) or not all(
            isinstance(preset, CameraPreset) for preset in self.camera_presets
        ):
            raise SceneValidationError("Shared PTZ presets must be an immutable typed tuple")
        if (
            not isinstance(self.pending_credential_deletions, tuple)
            or not all(
                isinstance(reference, str)
                and reference
                and len(reference) <= 512
                for reference in self.pending_credential_deletions
            )
            or len(self.pending_credential_deletions)
            != len(set(self.pending_credential_deletions))
        ):
            raise SceneValidationError("Invalid pending PTZ credential deletion queue")
        if len(self.cameras) > MAX_SHARED_CAMERAS:
            raise SceneValidationError("Too many shared cameras")
        if any(
            camera.kind not in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}
            or not isinstance(camera.configuration, (LocalCameraConfig, RtspCameraConfig))
            for camera in self.cameras
        ):
            raise SceneValidationError("Shared resources may contain only cameras")
        camera_ids = tuple(camera.id for camera in self.cameras)
        preset_ids = tuple(preset.id for preset in self.camera_presets)
        if len(camera_ids) != len(set(camera_ids)):
            raise SceneValidationError("Duplicate shared camera id")
        if len(preset_ids) != len(set(preset_ids)):
            raise SceneValidationError("Duplicate shared PTZ preset id")
        if any(preset.camera_source_id not in camera_ids for preset in self.camera_presets):
            raise SceneValidationError("Shared PTZ preset references a missing camera")

    def camera(self, camera_id: str) -> SourceDefinition:
        try:
            return next(camera for camera in self.cameras if camera.id == camera_id)
        except StopIteration as exc:
            raise SceneValidationError("Shared camera does not exist") from exc

    @property
    def credential_references(self) -> frozenset[str]:
        return frozenset(reference for camera in self.cameras
                         for reference in camera_credential_references(camera))

    def with_revision(self, revision: int) -> SceneResourceCatalog:
        return replace(self, revision=revision)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "cameras": [camera.to_record() for camera in self.cameras],
            "camera_presets": [preset.to_record() for preset in self.camera_presets],
            "pending_credential_deletions": list(self.pending_credential_deletions),
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneResourceCatalog:
        if not isinstance(raw, dict):
            raise SceneValidationError("Shared scene resources must be an object")
        schema_version = raw.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise SceneValidationError("Shared scene-resource schema version must be an integer")
        if schema_version not in {1, 2, SCENE_RESOURCE_SCHEMA_VERSION}:
            raise UnsupportedSceneResourceSchemaError(schema_version)
        data = _strict_mapping(
            raw,
            field_name="shared scene resources",
            allowed_keys={
                "schema_version",
                "revision",
                "cameras",
                "camera_presets",
                "pending_credential_deletions",
            },
        )
        revision = data.get("revision")
        cameras = data.get("cameras")
        presets = data.get("camera_presets", [])
        pending_credential_deletions = data.get("pending_credential_deletions", [])
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(cameras, list)
            or not isinstance(presets, list)
            or not isinstance(pending_credential_deletions, list)
        ):
            raise SceneValidationError("Invalid shared scene-resource record")
        return cls(
            schema_version=SCENE_RESOURCE_SCHEMA_VERSION,
            revision=revision,
            cameras=tuple(
                camera_source_from_legacy_record(item)
                if schema_version == 1
                else SourceDefinition.from_record(item)
                for item in cameras
            ),
            camera_presets=tuple(CameraPreset.from_record(item) for item in presets),
            pending_credential_deletions=tuple(pending_credential_deletions),
        )


def cameras_from_document_sources(
    sources: tuple[SourceDefinition, ...],
) -> tuple[SourceDefinition, ...]:
    return tuple(
        source
        for source in sources
        if source.kind in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}
    )


def _strict_mapping(
    raw: object,
    *,
    field_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SceneValidationError(f"{field_name} must be an object")
    unknown = set(raw) - allowed_keys
    if unknown:
        raise SceneValidationError(f"Unknown {field_name} fields: {', '.join(sorted(unknown))}")
    return dict(raw)
