from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from solin.core.scenes.engine import (
    EngineHealthEvent,
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
    OutputWindowTarget,
    SceneEngineHealth,
    SceneEngineSnapshot,
    SceneEngineStatus,
    SourceHealthEvent,
    SourceHealthStatus,
)
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DEFAULT_CAMERA_SOURCE_ID,
    BusId,
    CameraPreset,
    CameraMediaType,
    ContentCategory,
    ImageSourceConfig,
    LocalCameraConfig,
    OnvifPtzBinding,
    RtspCameraConfig,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    UnsupportedSceneSchemaError,
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
)
from solin.core.scenes.presets import (
    CAMERA_SCENE_ID,
    CONTENT_CAMERA_PIP_SCENE_ID,
    CONTENT_SCENE_ID,
    NO_SIGNAL_SCENE_ID,
    SceneSeedNames,
    create_default_scene_document,
)


def _names() -> SceneSeedNames:
    return SceneSeedNames(
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
    )


def _document() -> SceneDocument:
    return create_default_scene_document(
        _names(),
        document_id="test-scene-document",
        created_at="2026-08-02T12:00:00+00:00",
    )


def test_default_document_has_stable_sources_scenes_and_two_output_buses() -> None:
    first = _document()
    second = _document()

    assert first == second
    assert [source.id for source in first.sources] == [
        CONTENT_SOURCE_ID,
        DEFAULT_CAMERA_SOURCE_ID,
        "solin.source.no-signal",
    ]
    assert [scene.id for scene in first.scenes] == [
        CONTENT_SCENE_ID,
        CAMERA_SCENE_ID,
        CONTENT_CAMERA_PIP_SCENE_ID,
        NO_SIGNAL_SCENE_ID,
    ]
    assert {route.bus_id for route in first.outputs} == set(BusId)
    pip_scene = first.scene(CONTENT_CAMERA_PIP_SCENE_ID)
    assert [layer.source_id for layer in pip_scene.layers] == [
        CONTENT_SOURCE_ID,
        DEFAULT_CAMERA_SOURCE_ID,
    ]


def test_scene_document_round_trip_is_lossless() -> None:
    document = _document()

    restored = SceneDocument.from_record(document.to_record())

    assert restored == document
    assert restored.to_record() == document.to_record()


def test_schema_version_one_migrates_camera_frame_rates_without_mutating_input() -> None:
    record = _document().to_record()
    record["schema_version"] = 1
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    configuration = camera["configuration"]
    configuration.pop("fps_numerator")
    configuration.pop("fps_denominator")
    configuration.pop("media_type")
    configuration["fps"] = 30
    original = deepcopy(record)

    restored = SceneDocument.from_record(record)

    restored_camera = restored.source(DEFAULT_CAMERA_SOURCE_ID).configuration
    assert isinstance(restored_camera, LocalCameraConfig)
    assert restored.schema_version == 8
    assert restored_camera.fps_numerator == 0
    assert restored_camera.fps_denominator == 1
    assert record == original


def test_local_camera_frame_rate_is_rational_and_canonical() -> None:
    configuration = LocalCameraConfig(
        width=1920,
        height=1080,
        fps_numerator=30_000,
        fps_denominator=1_001,
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
    )

    assert LocalCameraConfig.from_record(configuration.to_record()) == configuration
    with pytest.raises(SceneValidationError, match="frame rate"):
        replace(configuration, fps_numerator=60_000, fps_denominator=2_002)
    with pytest.raises(SceneValidationError, match="denominator 1"):
        LocalCameraConfig(fps_numerator=0, fps_denominator=1_001)
    with pytest.raises(SceneValidationError, match="media type and pixel format"):
        LocalCameraConfig(media_type=CameraMediaType.RAW)
    with pytest.raises(SceneValidationError, match="media budget"):
        replace(configuration, width=3840, height=3840)
    with pytest.raises(SceneValidationError, match="media budget"):
        replace(configuration, width=2500, height=2500)
    with pytest.raises(SceneValidationError, match="frame rate"):
        replace(configuration, fps_numerator=120, fps_denominator=1)
    with pytest.raises(SceneValidationError, match="fully automatic or exact"):
        LocalCameraConfig(device_id="camera://device", width=1920, height=1080)


def test_schema_version_two_infers_the_camera_media_type() -> None:
    record = _document().to_record()
    record["schema_version"] = 2
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    configuration = camera["configuration"]
    configuration.pop("media_type")
    configuration.update(
        {
            "width": 1920,
            "height": 1080,
            "fps_numerator": 30,
            "fps_denominator": 1,
            "pixel_format": "JPEG",
        }
    )

    restored = SceneDocument.from_record(record)

    restored_camera = restored.source(DEFAULT_CAMERA_SOURCE_ID).configuration
    assert isinstance(restored_camera, LocalCameraConfig)
    assert restored.schema_version == 8
    assert restored_camera.media_type is CameraMediaType.JPEG


def test_schema_version_three_normalizes_legacy_camera_formats_outside_the_media_budget() -> None:
    record = _document().to_record()
    record["schema_version"] = 3
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    camera["configuration"].update(
        {
            "width": 3840,
            "height": 3840,
            "fps_numerator": 120,
            "fps_denominator": 1,
            "media_type": CameraMediaType.RAW.value,
            "pixel_format": "NV12",
        }
    )

    restored = SceneDocument.from_record(record)

    restored_camera = restored.source(DEFAULT_CAMERA_SOURCE_ID).configuration
    assert isinstance(restored_camera, LocalCameraConfig)
    assert restored.schema_version == 8
    assert restored_camera == LocalCameraConfig(device_id=restored_camera.device_id)


def test_schema_version_three_normalizes_legacy_partial_camera_formats() -> None:
    record = _document().to_record()
    record["schema_version"] = 3
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    camera["configuration"].update({"width": 1920, "height": 1080})

    restored = SceneDocument.from_record(record)

    restored_camera = restored.source(DEFAULT_CAMERA_SOURCE_ID).configuration
    assert isinstance(restored_camera, LocalCameraConfig)
    assert restored_camera == LocalCameraConfig(device_id=restored_camera.device_id)


def test_schema_version_four_repairs_the_legacy_camera_only_automation() -> None:
    record = _document().to_record()
    record["schema_version"] = 4
    virtual_camera = next(
        mapping
        for mapping in record["automation"]
        if mapping["bus_id"] == BusId.VIRTUAL_CAMERA.value
    )
    virtual_camera["assignments"] = {
        category.value: CAMERA_SCENE_ID for category in ContentCategory
    }

    restored = SceneDocument.from_record(record)

    automation = restored.automation_for(BusId.VIRTUAL_CAMERA)
    assert automation.scene_for(ContentCategory.IDLE) is None
    assert automation.scene_for(ContentCategory.IMAGE) == CONTENT_SCENE_ID
    assert automation.scene_for(ContentCategory.VIDEO) == CONTENT_SCENE_ID
    assert automation.scene_for(ContentCategory.TIMER) == CONTENT_SCENE_ID
    assert automation.scene_for(ContentCategory.CAMERA) is None


def test_schema_version_five_coalesces_duplicate_physical_camera_sources() -> None:
    record = _document().to_record()
    record["schema_version"] = 5
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    camera["configuration"]["device_id"] = "camera://physical-device"
    duplicate = deepcopy(camera)
    duplicate["id"] = "duplicate-camera"
    duplicate["name"] = "Duplicate camera"
    record["sources"].append(duplicate)
    record["scenes"][0]["layers"][0]["source_id"] = duplicate["id"]

    restored = SceneDocument.from_record(record)

    assert restored.schema_version == 8
    assert all(source.id != duplicate["id"] for source in restored.sources)
    assert restored.scenes[0].layers[0].source_id == DEFAULT_CAMERA_SOURCE_ID


def test_schema_version_five_normalizes_legacy_destinations_to_one_program() -> None:
    record = _document().to_record()
    record["schema_version"] = 5
    media_output = next(
        route
        for route in record["outputs"]
        if route["bus_id"] == BusId.MEDIA_WINDOWS.value
    )
    media_output["default_scene_id"] = CONTENT_SCENE_ID
    media_automation = next(
        mapping
        for mapping in record["automation"]
        if mapping["bus_id"] == BusId.MEDIA_WINDOWS.value
    )
    media_automation["assignments"] = {
        category.value: CONTENT_CAMERA_PIP_SCENE_ID
        for category in (
            ContentCategory.IMAGE,
            ContentCategory.VIDEO,
            ContentCategory.TIMER,
            ContentCategory.BROWSER,
            ContentCategory.EXTERNAL_STREAM,
        )
    }

    restored = SceneDocument.from_record(record)

    assert {
        route.default_scene_id for route in restored.outputs
    } == {CAMERA_SCENE_ID}
    assert {
        mapping.scene_for(ContentCategory.IMAGE)
        for mapping in restored.automation
    } == {CONTENT_SCENE_ID}


def test_scene_document_rejects_divergent_program_destinations() -> None:
    document = _document()
    media_route = document.output(BusId.MEDIA_WINDOWS)

    with pytest.raises(SceneValidationError, match="Program default"):
        replace(
            document,
            outputs=tuple(
                replace(route, default_scene_id=CONTENT_SCENE_ID)
                if route.bus_id is BusId.MEDIA_WINDOWS
                else route
                for route in document.outputs
            ),
        )

    with pytest.raises(SceneValidationError, match="Program media scene"):
        replace(
            document,
            automation=tuple(
                replace(
                    mapping,
                    assignments=tuple(
                        (category, CONTENT_CAMERA_PIP_SCENE_ID)
                        for category in (
                            ContentCategory.IMAGE,
                            ContentCategory.VIDEO,
                            ContentCategory.TIMER,
                            ContentCategory.BROWSER,
                            ContentCategory.EXTERNAL_STREAM,
                        )
                    ),
                )
                if mapping.bus_id is media_route.bus_id
                else mapping
                for mapping in document.automation
            ),
        )


def test_future_scene_schema_is_rejected_explicitly() -> None:
    record = _document().to_record()
    record["schema_version"] = 999

    with pytest.raises(UnsupportedSceneSchemaError) as error:
        SceneDocument.from_record(record)

    assert error.value.version == 999


def test_document_rejects_a_layer_that_references_a_missing_source() -> None:
    document = _document()
    scene = document.scene(CONTENT_SCENE_ID)
    invalid_scene = replace(
        scene,
        layers=(
            SceneLayer(
                id="missing-source-layer",
                source_id="missing-source",
                name="Missing",
            ),
        ),
    )

    with pytest.raises(SceneValidationError, match="missing source"):
        replace(
            document,
            scenes=tuple(
                invalid_scene if candidate.id == scene.id else candidate
                for candidate in document.scenes
            ),
        )


@pytest.mark.parametrize(
    "asset_name",
    ["../camera.png", "folder/camera.png", "folder\\camera.png", "C:\\camera.png"],
)
def test_image_assets_cannot_escape_the_profile_asset_directory(asset_name: str) -> None:
    with pytest.raises(SceneValidationError, match="image asset id"):
        ImageSourceConfig(asset_name)


def test_rtsp_credentials_are_never_persisted_in_source_endpoints() -> None:
    with pytest.raises(SceneValidationError, match="credentials"):
        RtspCameraConfig(uri="rtsp://operator:secret@camera.local/stream")

    with pytest.raises(SceneValidationError, match="credentials"):
        OnvifPtzBinding(
            endpoint="https://operator:secret@camera.local/onvif/device_service",
        )

    with pytest.raises(SceneValidationError, match="credentials"):
        RtspCameraConfig(uri="rtsp://camera.local/stream?access_token=secret")

    with pytest.raises(SceneValidationError, match="control characters"):
        RtspCameraConfig(uri="rtsp://camera.local/stream\n")

    with pytest.raises(SceneValidationError, match="credential reference"):
        replace(_document().sources[0], credential_ref="reference\ninvalid")


@pytest.mark.parametrize(
    "query_key",
    [
        "api_key",
        "apikey",
        "access-key",
        "auth",
        "authorization",
        "credential",
        "signature",
    ],
)
def test_rtsp_rejects_common_credential_query_names(query_key: str) -> None:
    with pytest.raises(SceneValidationError, match="credentials"):
        RtspCameraConfig(uri=f"rtsp://camera.local/stream?{query_key}=secret")


@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://camera.local:/stream",
        "rtsp://camera.local:0/stream",
        "rtsp://camera.local:abc/stream",
        "rtsp://camera.local:999999/stream",
    ],
)
def test_rtsp_rejects_invalid_explicit_ports(uri: str) -> None:
    with pytest.raises(SceneValidationError, match="RTSP URI"):
        RtspCameraConfig(uri=uri)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://camera.local:/onvif",
        "https://camera.local:0/onvif",
        "https://camera.local:abc/onvif",
        "https://camera.local:999999/onvif",
    ],
)
def test_onvif_rejects_invalid_explicit_ports(endpoint: str) -> None:
    with pytest.raises(SceneValidationError, match="ONVIF endpoint"):
        OnvifPtzBinding(endpoint=endpoint)


def test_source_configuration_must_match_its_source_kind() -> None:
    with pytest.raises(SceneValidationError, match="Configuration"):
        SourceDefinition(
            id="wrong-config",
            kind=SourceKind.IMAGE,
            name="Wrong",
            configuration=LocalCameraConfig(),
        )


def test_scene_reference_round_trips_and_accepts_an_acyclic_subscene() -> None:
    document = _document()
    source = SourceDefinition(
        id="camera-subscene-source",
        kind=SourceKind.SCENE_REFERENCE,
        name="Camera composition",
        configuration=SceneReferenceConfig(CAMERA_SCENE_ID),
    )
    content = document.scene(CONTENT_SCENE_ID)
    updated_content = replace(
        content,
        layers=(
            *content.layers,
            SceneLayer(
                id="camera-subscene-layer",
                source_id=source.id,
                name="Camera composition",
            ),
        ),
    )
    updated = replace(
        document,
        sources=(*document.sources, source),
        scenes=tuple(
            updated_content if scene.id == content.id else scene for scene in document.scenes
        ),
    )

    assert SceneDocument.from_record(updated.to_record()) == updated


def test_scene_reference_rejects_missing_targets_self_reference_and_indirect_cycles() -> None:
    document = _document()
    with pytest.raises(SceneValidationError, match="missing scene"):
        replace(
            document,
            sources=(
                *document.sources,
                SourceDefinition(
                    id="missing-scene-source",
                    kind=SourceKind.SCENE_REFERENCE,
                    name="Missing scene",
                    configuration=SceneReferenceConfig("missing-scene"),
                ),
            ),
        )

    content_reference = SourceDefinition(
        id="content-reference",
        kind=SourceKind.SCENE_REFERENCE,
        name="Content reference",
        configuration=SceneReferenceConfig(CONTENT_SCENE_ID),
    )
    content = document.scene(CONTENT_SCENE_ID)
    content_with_self_reference = replace(
        content,
        layers=(
            *content.layers,
            SceneLayer(
                id="self-reference-layer",
                source_id=content_reference.id,
                name="Self reference",
            ),
        ),
    )
    with pytest.raises(SceneValidationError, match="cycle"):
        replace(
            document,
            sources=(*document.sources, content_reference),
            scenes=tuple(
                content_with_self_reference if scene.id == content.id else scene
                for scene in document.scenes
            ),
        )

    camera_reference = SourceDefinition(
        id="camera-reference",
        kind=SourceKind.SCENE_REFERENCE,
        name="Camera reference",
        configuration=SceneReferenceConfig(CAMERA_SCENE_ID),
    )
    camera = document.scene(CAMERA_SCENE_ID)
    content_to_camera = replace(
        content,
        layers=(
            *content.layers,
            SceneLayer(
                id="content-to-camera",
                source_id=camera_reference.id,
                name="Camera reference",
            ),
        ),
    )
    camera_to_content = replace(
        camera,
        layers=(
            *camera.layers,
            SceneLayer(
                id="camera-to-content",
                source_id=content_reference.id,
                name="Content reference",
            ),
        ),
    )
    with pytest.raises(SceneValidationError, match="cycle"):
        replace(
            document,
            sources=(*document.sources, content_reference, camera_reference),
            scenes=tuple(
                content_to_camera
                if scene.id == content.id
                else camera_to_content
                if scene.id == camera.id
                else scene
                for scene in document.scenes
            ),
        )


def test_engine_snapshot_requires_each_bus_once_and_known_scenes() -> None:
    document = _document()
    valid = SceneEngineSnapshot(
        session_id="test-session",
        sequence=1,
        document=document,
        active_scenes=(
            (BusId.MEDIA_WINDOWS, CONTENT_SCENE_ID),
            (BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID),
        ),
        render_enabled=(
            (BusId.MEDIA_WINDOWS, False),
            (BusId.VIRTUAL_CAMERA, False),
        ),
        output_enabled=(
            (BusId.MEDIA_WINDOWS, False),
            (BusId.VIRTUAL_CAMERA, False),
        ),
    )
    assert valid.document is document

    with pytest.raises(ValueError, match="every output bus"):
        SceneEngineSnapshot(
            session_id="test-session",
            sequence=1,
            document=document,
            active_scenes=((BusId.MEDIA_WINDOWS, CONTENT_SCENE_ID),),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
        )

    with pytest.raises(ValueError, match="Window targets"):
        replace(
            valid,
            window_targets=tuple(
                OutputWindowTarget(
                    bus_id=BusId.MEDIA_WINDOWS,
                    target_id=f"window-{index}",
                    screen_id="screen-1",
                    native_handle=index + 1,
                    x=0,
                    y=0,
                    width=1920,
                    height=1080,
                    device_pixel_ratio=1.0,
                )
                for index in range(33)
            ),
        )
    with pytest.raises(ValueError, match="window target id"):
        OutputWindowTarget(
            bus_id=BusId.MEDIA_WINDOWS,
            target_id="window\ninvalid",
            screen_id="screen-1",
            native_handle=1,
            x=0,
            y=0,
            width=1920,
            height=1080,
            device_pixel_ratio=1.0,
        )

    with pytest.raises(ValueError, match="unknown active scene"):
        SceneEngineSnapshot(
            session_id="test-session",
            sequence=1,
            document=document,
            active_scenes=(
                (BusId.MEDIA_WINDOWS, CONTENT_SCENE_ID),
                (BusId.VIRTUAL_CAMERA, "missing-scene"),
            ),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
        )

    with pytest.raises(ValueError, match="every output bus"):
        SceneEngineSnapshot(
            session_id="test-session",
            sequence=1,
            document=document,
            active_scenes=(
                (BusId.MEDIA_WINDOWS, CONTENT_SCENE_ID),
                (BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID),
                (BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID),
            ),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
            ),
        )


@pytest.mark.parametrize(
    ("transport", "pixel_format", "accepted"),
    [
        (FrameChannelTransport.SHARED_MEMORY_BGRA, VideoPixelFormat.BGRA, True),
        (FrameChannelTransport.SHARED_MEMORY_BGRA, VideoPixelFormat.NV12, False),
        (FrameChannelTransport.SHARED_MEMORY_VIDEO, VideoPixelFormat.DYNAMIC, True),
        (FrameChannelTransport.SHARED_MEMORY_VIDEO, VideoPixelFormat.BGRA, False),
        (FrameChannelTransport.D3D11_SHARED_TEXTURE, VideoPixelFormat.BGRA, True),
        (FrameChannelTransport.D3D11_SHARED_TEXTURE, VideoPixelFormat.NV12, True),
    ],
)
def test_frame_channel_transport_requires_a_compatible_pixel_layout(
    transport: FrameChannelTransport,
    pixel_format: VideoPixelFormat,
    accepted: bool,
) -> None:
    arguments = {
        "channel_id": "content-1",
        "generation": 1,
        "producer_kind": FrameProducerKind.SOLIN_OFFSCREEN,
        "transport": transport,
        "handle_token": "handle-1",
        "width": 1920,
        "height": 1080,
        "pixel_format": pixel_format,
        "color_space": VideoColorSpace.BT709,
        "color_range": VideoColorRange.FULL,
    }
    if accepted:
        assert FrameChannelDescriptor(**arguments).pixel_format is pixel_format
    else:
        with pytest.raises(ValueError, match="require"):
            FrameChannelDescriptor(**arguments)


def test_camera_preset_rejects_control_characters_in_remote_tokens() -> None:
    with pytest.raises(SceneValidationError, match="token"):
        CameraPreset(
            id="preset-1",
            camera_source_id=DEFAULT_CAMERA_SOURCE_ID,
            name="Preset",
            remote_token="remote\ninvalid",
        )


def test_engine_health_events_reject_invalid_process_boundary_values() -> None:
    health = SceneEngineHealth(
        status=SceneEngineStatus.READY,
        session_id="session-1",
        process_generation="generation-1",
        process_id=42,
        last_heartbeat_monotonic=123.5,
        restart_count=0,
    )
    assert EngineHealthEvent(health).health is health

    with pytest.raises(ValueError, match="heartbeat"):
        replace(health, last_heartbeat_monotonic=float("nan"))
    with pytest.raises(ValueError, match="error code"):
        SourceHealthEvent(
            source_id="camera-1",
            status=SourceHealthStatus.FAILED,
        )
