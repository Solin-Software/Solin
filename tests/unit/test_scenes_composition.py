from __future__ import annotations

from dataclasses import replace

import pytest

from solin.core.scenes.composition import (
    SceneComposition,
    analyze_scene_compositions,
    scene_ptz_camera_ids,
    scene_uses_content_source,
)
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    LocalCameraConfig,
    OnvifPtzBinding,
    SceneDefinition,
    SolinContentConfig,
    SolinContentMode,
    SceneLayer,
    SceneReferenceConfig,
    SourceDefinition,
    SourceKind,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    NO_SIGNAL_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)


def _document():
    return create_default_scene_document(
        SceneSeedNames(
            content_source="Current content",
            default_camera_source="Default camera",
            no_signal_source="No signal background",
            content_scene="Content",
            camera_scene="Camera",
            content_camera_pip_scene="Content + camera",
            no_signal_scene="No signal",
            content_layer="Content",
            camera_layer="Camera",
            background_layer="Background",
        ),
        document_id="composition-test",
        created_at="2026-08-28T12:00:00+00:00",
    )


def test_scene_composition_identifies_camera_above_content() -> None:
    document = _document()

    compositions = analyze_scene_compositions(document)

    assert compositions[CONTENT_SCENE_ID] == SceneComposition(uses_content=True)
    assert compositions[CAMERA_SCENE_ID] == SceneComposition(uses_camera=True)
    assert compositions[CONTENT_CAMERA_PIP_SCENE_ID] == SceneComposition(
        uses_content=True,
        uses_camera=True,
        has_camera_over_content=True,
    )
    assert compositions[NO_SIGNAL_SCENE_ID] == SceneComposition()
    assert scene_uses_content_source(document, CONTENT_CAMERA_PIP_SCENE_ID)


def test_scene_composition_respects_authored_layer_order() -> None:
    document = _document()
    pip_scene = document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    reversed_scene = replace(pip_scene, layers=tuple(reversed(pip_scene.layers)))
    document = replace(
        document,
        scenes=tuple(
            reversed_scene if scene.id == reversed_scene.id else scene
            for scene in document.scenes
        ),
    )

    composition = analyze_scene_compositions(document)[reversed_scene.id]

    assert composition.uses_content
    assert composition.uses_camera
    assert not composition.has_camera_over_content


def test_scene_composition_ignores_hidden_and_disabled_camera_layers() -> None:
    document = _document()
    pip_scene = document.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    hidden_scene = replace(
        pip_scene,
        layers=(pip_scene.layers[0], replace(pip_scene.layers[1], visible=False)),
    )
    hidden_document = replace(
        document,
        scenes=tuple(
            hidden_scene if scene.id == hidden_scene.id else scene
            for scene in document.scenes
        ),
    )
    disabled_document = replace(
        document,
        sources=tuple(
            replace(source, enabled=False)
            if source.id == DEFAULT_CAMERA_SOURCE_ID
            else source
            for source in document.sources
        ),
    )

    assert not analyze_scene_compositions(hidden_document)[
        CONTENT_CAMERA_PIP_SCENE_ID
    ].has_camera_over_content
    assert not analyze_scene_compositions(disabled_document)[
        CONTENT_CAMERA_PIP_SCENE_ID
    ].has_camera_over_content


def test_scene_composition_follows_nested_camera_scenes() -> None:
    document = _document()
    reference = SourceDefinition(
        id="nested-camera-source",
        kind=SourceKind.SCENE_REFERENCE,
        name="Nested camera",
        configuration=SceneReferenceConfig(target_scene_id=CAMERA_SCENE_ID),
    )
    scene = SceneDefinition(
        id="nested-camera-pip",
        name="Nested camera PiP",
        layers=(
            SceneLayer(
                id="nested-content-layer",
                source_id=CONTENT_SOURCE_ID,
                name="Content",
            ),
            SceneLayer(
                id="nested-camera-layer",
                source_id=reference.id,
                name="Nested camera",
            ),
        ),
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:00+00:00",
    )
    document = replace(
        document,
        sources=(*document.sources, reference),
        scenes=(*document.scenes, scene),
    )

    assert analyze_scene_compositions(document)[scene.id] == SceneComposition(
        uses_content=True,
        uses_camera=True,
        has_camera_over_content=True,
    )


def test_nested_pip_composition_remains_classified_through_a_scene_reference() -> None:
    document = _document()
    reference = SourceDefinition(
        id="nested-pip-source",
        kind=SourceKind.SCENE_REFERENCE,
        name="Nested PiP",
        configuration=SceneReferenceConfig(
            target_scene_id=CONTENT_CAMERA_PIP_SCENE_ID,
        ),
    )
    scene = SceneDefinition(
        id="nested-pip-scene",
        name="Nested PiP",
        layers=(
            SceneLayer(
                id="nested-pip-layer",
                source_id=reference.id,
                name="Nested PiP",
            ),
        ),
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:00+00:00",
    )
    document = replace(
        document,
        sources=(*document.sources, reference),
        scenes=(*document.scenes, scene),
    )

    assert analyze_scene_compositions(document)[scene.id].has_camera_over_content


@pytest.mark.parametrize("mode", list(SolinContentMode))
def test_content_composition_uses_source_type_and_mode_with_arbitrary_identity(mode):
    document = _document()
    changed_id = "user.content"
    sources = tuple(replace(source, id=changed_id, configuration=SolinContentConfig(mode)) if source.id == CONTENT_SOURCE_ID else source for source in document.sources)
    scenes = tuple(replace(scene, layers=tuple(replace(layer, source_id=changed_id) if layer.source_id == CONTENT_SOURCE_ID else layer for layer in scene.layers)) for scene in document.scenes)
    changed = replace(document, sources=sources, scenes=scenes)
    assert analyze_scene_compositions(changed) == analyze_scene_compositions(document)


def _with_ptz_camera(document):
    return replace(
        document,
        sources=tuple(
            replace(
                source,
                configuration=LocalCameraConfig(
                    ptz_binding=OnvifPtzBinding(endpoint="http://camera.test/onvif/ptz_service"),
                ),
            )
            if source.id == DEFAULT_CAMERA_SOURCE_ID
            else source
            for source in document.sources
        ),
    )


def _wrapping_scene(scene_id: str, target_scene_id: str, reference_id: str):
    reference = SourceDefinition(
        id=reference_id,
        kind=SourceKind.SCENE_REFERENCE,
        name="Nested",
        configuration=SceneReferenceConfig(target_scene_id=target_scene_id),
    )
    scene = SceneDefinition(
        id=scene_id,
        name=scene_id,
        layers=(SceneLayer(id=f"{scene_id}-layer", source_id=reference.id, name="Nested"),),
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:00+00:00",
    )
    return reference, scene


def test_ptz_cameras_are_found_directly_and_through_nested_scenes() -> None:
    document = _with_ptz_camera(_document())
    reference, wrapper = _wrapping_scene("wrapper", CAMERA_SCENE_ID, "wrapper-reference")
    document = replace(
        document,
        sources=(*document.sources, reference),
        scenes=(*document.scenes, wrapper),
    )

    assert scene_ptz_camera_ids(document, CAMERA_SCENE_ID) == (DEFAULT_CAMERA_SOURCE_ID,)
    assert scene_ptz_camera_ids(document, "wrapper") == (DEFAULT_CAMERA_SOURCE_ID,)
    assert scene_ptz_camera_ids(document, CONTENT_SCENE_ID) == ()
    assert scene_ptz_camera_ids(document, "missing-scene") == ()


def test_cameras_without_ptz_are_not_listed() -> None:
    assert scene_ptz_camera_ids(_document(), CAMERA_SCENE_ID) == ()
