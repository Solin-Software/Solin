from __future__ import annotations

from dataclasses import dataclass

from solin.core.scenes.model import (
    LocalCameraConfig,
    RtspCameraConfig,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
)


_CAMERA_SOURCE_KINDS = frozenset(
    {
        SourceKind.LOCAL_CAMERA,
        SourceKind.RTSP_CAMERA,
    }
)


@dataclass(frozen=True, slots=True)
class SceneComposition:
    """Visible source roles and their authored stacking relationship."""

    uses_content: bool = False
    uses_camera: bool = False
    has_camera_over_content: bool = False


_EMPTY_COMPOSITION = SceneComposition()
_CONTENT_COMPOSITION = SceneComposition(uses_content=True)
_CAMERA_COMPOSITION = SceneComposition(uses_camera=True)


def analyze_scene_compositions(
    document: SceneDocument,
) -> dict[str, SceneComposition]:
    """Analyze every scene once, including visible nested-scene composition."""

    analyzer = _SceneCompositionAnalyzer(document)
    return {scene.id: analyzer.analyze(scene.id) for scene in document.scenes}


def scene_uses_content_source(document: SceneDocument, scene_id: str) -> bool:
    return _SceneCompositionAnalyzer(document).analyze(scene_id).uses_content


class _SceneCompositionAnalyzer:
    def __init__(self, document: SceneDocument) -> None:
        self._sources = {source.id: source for source in document.sources}
        self._scenes = {scene.id: scene for scene in document.scenes}
        self._compositions: dict[str, SceneComposition] = {}
        self._visiting: set[str] = set()

    def analyze(self, scene_id: str) -> SceneComposition:
        cached = self._compositions.get(scene_id)
        if cached is not None:
            return cached
        if scene_id in self._visiting:
            return _EMPTY_COMPOSITION
        self._visiting.add(scene_id)

        uses_content = False
        uses_camera = False
        has_camera_over_content = False
        content_below = False
        for layer in self._scenes[scene_id].layers:
            source = self._sources[layer.source_id]
            if not _layer_is_active(layer, source):
                continue
            layer_composition = self._source_composition(source)
            if layer_composition.has_camera_over_content or (
                content_below and layer_composition.uses_camera
            ):
                has_camera_over_content = True
            uses_content = uses_content or layer_composition.uses_content
            uses_camera = uses_camera or layer_composition.uses_camera
            content_below = content_below or layer_composition.uses_content

        self._visiting.remove(scene_id)
        result = SceneComposition(
            uses_content=uses_content,
            uses_camera=uses_camera,
            has_camera_over_content=has_camera_over_content,
        )
        self._compositions[scene_id] = result
        return result

    def _source_composition(self, source: SourceDefinition) -> SceneComposition:
        if source.kind is SourceKind.SOLIN_CONTENT:
            return _CONTENT_COMPOSITION
        if source.kind in _CAMERA_SOURCE_KINDS:
            return _CAMERA_COMPOSITION
        configuration = source.configuration
        if isinstance(configuration, SceneReferenceConfig):
            return self.analyze(configuration.target_scene_id)
        return _EMPTY_COMPOSITION


def scene_ptz_camera_ids(document: SceneDocument, scene_id: str) -> tuple[str, ...]:
    """The PTZ-capable cameras a scene shows, nested scenes included, in layer order.

    Hidden layers count: PTZ belongs to the camera, and a layer toggled visible a
    moment later must not find its controls missing.
    """
    sources = {source.id: source for source in document.sources}
    scenes = {scene.id: scene for scene in document.scenes}
    result: list[str] = []
    visited: set[str] = set()

    def visit_scene(target_id: str) -> None:
        scene = scenes.get(target_id)
        if scene is None or target_id in visited:
            return
        visited.add(target_id)
        for layer in scene.layers:
            source = sources.get(layer.source_id)
            if source is None:
                continue
            configuration = source.configuration
            if isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
                if configuration.ptz_binding is not None and source.id not in result:
                    result.append(source.id)
            elif isinstance(configuration, SceneReferenceConfig):
                visit_scene(configuration.target_scene_id)

    visit_scene(scene_id)
    return tuple(result)


def _layer_is_active(layer: SceneLayer, source: SourceDefinition) -> bool:
    return (
        source.enabled
        and layer.visible
        and layer.opacity > 0.0
        and layer.rect.width > 0.0
        and layer.rect.height > 0.0
    )
