from __future__ import annotations

from dataclasses import dataclass, replace

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
    YeartextSourceConfig,
    stable_identity,
    utc_now_iso,
)


# The year text is rendered by the app in its own styling and shown as an image
# source; there is a single global instance, so it has a stable well-known id.
YEARTEXT_SOURCE_ID = "solin.yeartext.current"

CONTENT_SCENE_ID = stable_identity("default-scene:content")
CAMERA_SCENE_ID = stable_identity("default-scene:camera")
CONTENT_CAMERA_PIP_SCENE_ID = stable_identity("default-scene:content-camera-pip")
NO_SIGNAL_SCENE_ID = stable_identity("default-scene:no-signal")
# The idle-fallback scene ("default" label): shown whenever nothing else is
# presented. On first run it holds the full-size year text and is the default.
DEFAULT_SCENE_ID = stable_identity("default-scene:default")


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
    yeartext_source: str = "Year text"
    default_scene: str = "Default"
    yeartext_layer: str = "Year text"


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
                default_scene_id=CAMERA_SCENE_ID,
                video_format=VideoFormat(),
            ),
            OutputRoute(
                bus_id=BusId.VIRTUAL_CAMERA,
                default_scene_id=CAMERA_SCENE_ID,
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
) -> SceneDocument:
    """Create the minimal first Scene profile.

    The first-run collection is a **Default** scene (the idle fallback, holding the
    full-size year text) plus Camera and Content. The Default scene is the shared
    ``default_scene_id`` for every output bus, so whenever nothing else is
    presented the year text shows on both the projection and the virtual camera.
    The year-text source ships only here (fresh installs); other collections add
    it on demand via the source picker.
    """

    timestamp = created_at or utc_now_iso()
    document = create_default_scene_document(
        names,
        document_id=document_id,
        created_at=created_at,
    )
    yeartext_source = SourceDefinition(
        id=YEARTEXT_SOURCE_ID,
        kind=SourceKind.YEARTEXT,
        name=names.yeartext_source,
        configuration=YeartextSourceConfig(),
    )
    yeartext_layer = _layer(
        "default:yeartext",
        YEARTEXT_SOURCE_ID,
        names.yeartext_layer,
        fit_mode=FitMode.CONTAIN,
    )
    default_scene = SceneDefinition(
        id=DEFAULT_SCENE_ID,
        name=names.default_scene,
        layers=(yeartext_layer,),
        created_at=timestamp,
        updated_at=timestamp,
    )
    return replace(
        document,
        sources=(*document.sources, yeartext_source),
        scenes=(
            default_scene,
            document.scene(CAMERA_SCENE_ID),
            document.scene(CONTENT_SCENE_ID),
        ),
        outputs=tuple(
            replace(route, default_scene_id=DEFAULT_SCENE_ID)
            for route in document.outputs
        ),
    )


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
