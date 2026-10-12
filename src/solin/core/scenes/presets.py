from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    NO_SIGNAL_SOURCE_ID,
    AutomationMap,
    BusId,
    ColorSourceConfig,
    ContentCategory,
    FitMode,
    LocalCameraConfig,
    NormalizedRect,
    OutputRoute,
    SceneDefinition,
    SceneDocument,
    SceneLayer,
    SceneTransitionPolicy,
    SolinContentConfig,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
    VideoColorRange,
    VideoColorSpace,
    VideoFormat,
    VideoPixelFormat,
    stable_identity,
    utc_now_iso,
)


CONTENT_SCENE_ID = stable_identity("default-scene:content")
CAMERA_SCENE_ID = stable_identity("default-scene:camera")
CONTENT_CAMERA_PIP_SCENE_ID = stable_identity("default-scene:content-camera-pip")
NO_SIGNAL_SCENE_ID = stable_identity("default-scene:no-signal")
# Frozen schema-11 bootstrap vocabulary from the 18 shipped _Scenes catalogs.
# Forward migration cannot rely on TS files or vanished entries in current QM files.
_BETA_PRESET_NAMES: dict[str, frozenset[str]] = {
    'Current Solin content': frozenset({
        'Current Solin content',
    }),
    'Content': frozenset({
        'Content',
    }),
    'Camera': frozenset({
        'Camera',
    }),
    'Default camera': frozenset({
        'Default camera',
    }),
    'No signal background': frozenset({
        'No signal background',
    }),
    'Idle screen': frozenset({
        'Ekran bezczynności',
        'Idle Screen',
        'Idle screen',
        'Inaktiv skärm',
        'Layar Siaga',
        'Pantalla inactiva',
        'Ruhebild',
        'Schermata inattiva',
        'Skrini Isiyotumika',
        'Stand-byscherm',
        'Tela de descanso',
        'Valmiusnäyttö',
        'Écran inactif',
        'Неактивный экран',
        'Фоновий екран',
        'アイドル画面',
        '空闲屏幕',
        '비활성 화면',
    }),
    'Default': frozenset({
        'Default',
    }),
}


@dataclass(frozen=True, slots=True)
class SceneSeedNames:
    content_source: str
    default_camera_source: str
    no_signal_source: str
    content_scene: str
    camera_scene: str
    content_camera_pip_scene: str
    no_signal_scene: str
    content_layer: str
    camera_layer: str
    background_layer: str


def create_default_scene_document(
    names: SceneSeedNames,
    *,
    document_id: str,
    created_at: str | None = None,
) -> SceneDocument:
    timestamp = created_at or utc_now_iso()
    content_layer = _layer(
        "content:content",
        CONTENT_SOURCE_ID,
        names.content_layer,
        fit_mode=FitMode.CONTAIN,
    )
    camera_layer = _layer(
        "camera:camera",
        DEFAULT_CAMERA_SOURCE_ID,
        names.camera_layer,
    )
    pip_content_layer = _layer(
        "pip:content",
        CONTENT_SOURCE_ID,
        names.content_layer,
        fit_mode=FitMode.CONTAIN,
    )
    pip_camera_layer = _layer(
        "pip:camera",
        DEFAULT_CAMERA_SOURCE_ID,
        names.camera_layer,
        rect=NormalizedRect(x=0.69, y=0.66, width=0.28, height=0.28),
        border_color="#FFFFFFFF",
        border_width=0.004,
        corner_radius=0.025,
    )
    no_signal_layer = _layer(
        "no-signal:background",
        NO_SIGNAL_SOURCE_ID,
        names.background_layer,
        fit_mode=FitMode.STRETCH,
    )

    scenes = (
        SceneDefinition(
            id=CONTENT_SCENE_ID,
            name=names.content_scene,
            layers=(content_layer,),
            created_at=timestamp,
            updated_at=timestamp,
        ),
        SceneDefinition(
            id=CAMERA_SCENE_ID,
            name=names.camera_scene,
            layers=(camera_layer,),
            created_at=timestamp,
            updated_at=timestamp,
        ),
        SceneDefinition(
            id=CONTENT_CAMERA_PIP_SCENE_ID,
            name=names.content_camera_pip_scene,
            layers=(pip_content_layer, pip_camera_layer),
            created_at=timestamp,
            updated_at=timestamp,
        ),
        SceneDefinition(
            id=NO_SIGNAL_SCENE_ID,
            name=names.no_signal_scene,
            layers=(no_signal_layer,),
            created_at=timestamp,
            updated_at=timestamp,
        ),
    )
    return SceneDocument(
        document_id=document_id,
        revision=0,
        sources=(
            SourceDefinition(
                id=CONTENT_SOURCE_ID,
                kind=SourceKind.SOLIN_CONTENT,
                name=names.content_source,
                configuration=SolinContentConfig(),
            ),
            SourceDefinition(
                id=DEFAULT_CAMERA_SOURCE_ID,
                kind=SourceKind.LOCAL_CAMERA,
                name=names.default_camera_source,
                configuration=LocalCameraConfig(),
            ),
            SourceDefinition(
                id=NO_SIGNAL_SOURCE_ID,
                kind=SourceKind.COLOR,
                name=names.no_signal_source,
                configuration=ColorSourceConfig(),
            ),
        ),
        scenes=scenes,
        outputs=(
            OutputRoute(
                bus_id=BusId.MEDIA_WINDOWS,
                video_format=VideoFormat(),
            ),
            OutputRoute(
                bus_id=BusId.VIRTUAL_CAMERA,
                video_format=VideoFormat(
                    fps_numerator=30,
                    pixel_format=VideoPixelFormat.NV12,
                    color_space=VideoColorSpace.BT709,
                    color_range=VideoColorRange.LIMITED,
                ),
            ),
        ),
        automation=(
            AutomationMap(
                bus_id=BusId.MEDIA_WINDOWS,
                assignments=(
                    (ContentCategory.IMAGE, CONTENT_SCENE_ID),
                    (ContentCategory.VIDEO, CONTENT_SCENE_ID),
                    (ContentCategory.TIMER, CONTENT_SCENE_ID),
                    (ContentCategory.BROWSER, CONTENT_SCENE_ID),
                    (ContentCategory.EXTERNAL_STREAM, CONTENT_SCENE_ID),
                ),
            ),
            AutomationMap(
                bus_id=BusId.VIRTUAL_CAMERA,
                assignments=(
                    (ContentCategory.IMAGE, CONTENT_SCENE_ID),
                    (ContentCategory.VIDEO, CONTENT_SCENE_ID),
                    (ContentCategory.TIMER, CONTENT_SCENE_ID),
                    (ContentCategory.BROWSER, CONTENT_SCENE_ID),
                    (ContentCategory.EXTERNAL_STREAM, CONTENT_SCENE_ID),
                ),
            ),
        ),
        transition_policy=SceneTransitionPolicy(
            default=TransitionSpec(TransitionKind.DISSOLVE, 350)
        ),
    )


def create_fresh_scene_collection_document(
    names: SceneSeedNames,
    *,
    document_id: str,
    created_at: str | None = None,
    camera: SourceDefinition | None = None,
) -> SceneDocument:
    """Create Media with one presentation layer and Camera, empty until configured."""
    if camera is not None and camera.kind not in {SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA}:
        raise ValueError("A fresh Camera scene requires a camera source")
    document = create_default_scene_document(names, document_id=document_id, created_at=created_at)
    camera_scene = document.scene(CAMERA_SCENE_ID)
    camera_scene = replace(camera_scene, layers=() if camera is None else tuple(replace(layer, source_id=camera.id) for layer in camera_scene.layers))
    sources = tuple(source for source in document.sources if source.id != DEFAULT_CAMERA_SOURCE_ID)
    if camera is not None:
        sources = (*sources, camera)
    return replace(document, sources=sources, scenes=(document.scene(CONTENT_SCENE_ID), camera_scene))


def convert_pristine_beta_preset(
    document: SceneDocument,
    names: SceneSeedNames,
    *,
    selections: dict[BusId, str],
) -> SceneDocument:
    """Rebuild only an exact shipped two/three-scene preset after beta decoding."""
    old_default_id = stable_identity("default-scene:default")
    scene_ids = tuple(scene.id for scene in document.scenes)
    two_scene = set(scene_ids) == {CAMERA_SCENE_ID, CONTENT_SCENE_ID}
    three_scene = set(scene_ids) == {old_default_id, CAMERA_SCENE_ID, CONTENT_SCENE_ID}
    if not two_scene and not three_scene:
        return document
    expected_base = old_default_id if three_scene else CAMERA_SCENE_ID
    if set(selections) != {BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA} or set(selections.values()) != {expected_base}:
        return document
    # Beta display names are matched against the shipped vocabulary separately
    # from the new preset labels. Arbitrary user names must never trigger rebuilds.
    content = next((source for source in document.sources if source.id == CONTENT_SOURCE_ID), None)
    if content is None:
        return document
    content_scene = document.scene(CONTENT_SCENE_ID)
    if content.name not in _BETA_PRESET_NAMES["Current Solin content"]:
        return document
    if content_scene.name not in _BETA_PRESET_NAMES["Content"] or len(content_scene.layers) != 1:
        return document
    if content_scene.layers[0].name not in _BETA_PRESET_NAMES["Content"]:
        return document
    camera_scene = document.scene(CAMERA_SCENE_ID)
    if camera_scene.name not in _BETA_PRESET_NAMES["Camera"] or len(camera_scene.layers) != 1:
        return document
    if camera_scene.layers[0].name not in _BETA_PRESET_NAMES["Camera"]:
        return document
    camera = next((source for source in document.sources if source.id == DEFAULT_CAMERA_SOURCE_ID), None)
    if camera is None or camera.name not in _BETA_PRESET_NAMES["Default camera"]:
        return document
    no_signal = next((source for source in document.sources if source.id == NO_SIGNAL_SOURCE_ID), None)
    if no_signal is None or no_signal.name not in _BETA_PRESET_NAMES["No signal background"]:
        return document
    beta_names = replace(
        names, content_source=content.name, content_scene=content_scene.name,
        content_layer=content_scene.layers[0].name, camera_scene=camera_scene.name,
        camera_layer=camera_scene.layers[0].name, default_camera_source=camera.name,
        no_signal_source=no_signal.name,
    )
    seed = create_default_scene_document(beta_names, document_id=document.document_id)
    expected = replace(seed, scenes=(seed.scene(CONTENT_SCENE_ID), seed.scene(CAMERA_SCENE_ID)))
    if three_scene:
        idle = next((source for source in document.sources if source.id == "solin.idle.current"), None)
        default_scene = document.scene(old_default_id)
        if idle is None or idle.name not in _BETA_PRESET_NAMES["Idle screen"]:
            return document
        if default_scene.name not in _BETA_PRESET_NAMES["Default"] or len(default_scene.layers) != 1:
            return document
        if default_scene.layers[0].name not in _BETA_PRESET_NAMES["Idle screen"]:
            return document
        idle_source = SourceDefinition(
            id=idle.id, kind=SourceKind.SOLIN_CONTENT,
            name=idle.name, configuration=SolinContentConfig(),
        )
        old_default = SceneDefinition(
            id=old_default_id, name=default_scene.name,
            layers=(_layer("default:idle", idle_source.id, default_scene.layers[0].name, fit_mode=FitMode.CONTAIN),),
            created_at=document.scenes[0].created_at, updated_at=document.scenes[0].updated_at,
        )
        expected = replace(expected, sources=(*expected.sources, idle_source), scenes=(old_default, expected.scene(CAMERA_SCENE_ID), expected.scene(CONTENT_SCENE_ID)))
    else:
        expected = replace(expected, scenes=(expected.scene(CAMERA_SCENE_ID), expected.scene(CONTENT_SCENE_ID)))
    def structural_record(value: SceneDocument) -> dict[str, Any]:
        record: dict[str, Any] = value.to_record()
        record.pop("revision")
        for scene in record["scenes"]:
            scene.pop("created_at")
            scene.pop("updated_at")
        return record
    if structural_record(document) != structural_record(expected):
        return document
    fresh = create_fresh_scene_collection_document(names, document_id=document.document_id, created_at=document.scenes[0].created_at)
    return replace(fresh, revision=document.revision)


def _layer(
    seed: str,
    source_id: str,
    name: str,
    *,
    rect: NormalizedRect | None = None,
    fit_mode: FitMode = FitMode.COVER,
    border_color: str = "#00000000",
    border_width: float = 0.0,
    corner_radius: float = 0.0,
) -> SceneLayer:
    return SceneLayer(
        id=stable_identity(f"default-layer:{seed}"),
        source_id=source_id,
        name=name,
        rect=rect or NormalizedRect(),
        fit_mode=fit_mode,
        border_color=border_color,
        border_width=border_width,
        corner_radius=corner_radius,
    )
