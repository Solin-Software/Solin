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
    scene_engine_graph_signature,
    SceneEngineHealth,
    SceneEngineSnapshot,
    SceneEngineStatus,
    SourceHealthEvent,
    SourceHealthStatus,
)
from solin.core.scenes.model import (
    CONTENT_SOURCE_ID,
    DELIVERY_BUSES,
    DEFAULT_CAMERA_SOURCE_ID,
    BusId,
    CameraPreset,
    CameraMediaType,
    ContentCategory,
    FitMode,
    ImageSourceConfig,
    LocalCameraConfig,
    NormalizedRect,
    OnvifPtzBinding,
    RtspCameraConfig,
    SCHEMA_VERSION,
    SceneDocument,
    SceneLayer,
    SceneReferenceConfig,
    SceneTransitionPolicy,
    SceneValidationError,
    SourceDefinition,
    SourceKind,
    TransitionKind,
    TransitionSpec,
    UnsupportedSceneSchemaError,
    VideoColorRange,
    VideoColorSpace,
    VideoPixelFormat,
    SolinContentConfig,
    SolinContentMode,
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


def test_new_layer_and_record_without_fit_contain_the_complete_source() -> None:
    layer = SceneLayer(id="layer", source_id="source", name="Source")
    assert layer.fit_mode is FitMode.CONTAIN
    record = layer.to_record()
    del record["fit_mode"]
    assert SceneLayer.from_record(record).fit_mode is FitMode.CONTAIN


@pytest.mark.parametrize("fit_mode", tuple(FitMode))
def test_saved_layer_preserves_its_explicit_fit_mode(fit_mode: FitMode) -> None:
    layer = SceneLayer(id="layer", source_id="source", name="Source", fit_mode=fit_mode)
    assert SceneLayer.from_record(layer.to_record()) == layer


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
    assert {route.bus_id for route in first.outputs} == set(DELIVERY_BUSES)
    assert first.transition_policy.default == TransitionSpec(
        TransitionKind.DISSOLVE,
        350,
    )
    assert first.transition_policy.overrides == ()
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


def test_transition_spec_enforces_cut_and_animated_duration_contracts() -> None:
    assert TransitionSpec(TransitionKind.CUT, 0).duration_ms == 0
    assert TransitionSpec(TransitionKind.DISSOLVE, 50).duration_ms == 50
    assert TransitionSpec(TransitionKind.FADE_TO_BLACK, 10_000).duration_ms == 10_000
    assert TransitionSpec(TransitionKind.MERGE, 350).to_record() == {
        "kind": "merge",
        "duration_ms": 350,
    }
    assert TransitionSpec.from_record({"kind": "merge", "duration_ms": 350}).kind is TransitionKind.MERGE

    with pytest.raises(SceneValidationError, match="Cut transition duration"):
        TransitionSpec(TransitionKind.CUT, 50)
    with pytest.raises(SceneValidationError, match="between 50 and 10000"):
        TransitionSpec(TransitionKind.DISSOLVE, 49)
    with pytest.raises(SceneValidationError, match="between 50 and 10000"):
        TransitionSpec(TransitionKind.FADE_TO_BLACK, 10_001)
    with pytest.raises(SceneValidationError, match="must be an integer"):
        TransitionSpec(TransitionKind.DISSOLVE, True)


def test_transition_policy_round_trip_and_effective_resolution_are_typed() -> None:
    policy = SceneTransitionPolicy(
        default=TransitionSpec(TransitionKind.DISSOLVE, 350),
        overrides=(
            (CONTENT_SCENE_ID, TransitionSpec(TransitionKind.FADE_TO_BLACK, 500)),
        ),
    )

    restored = SceneTransitionPolicy.from_record(policy.to_record())

    assert restored == policy
    assert restored.effective_for(CONTENT_SCENE_ID) == TransitionSpec(
        TransitionKind.FADE_TO_BLACK,
        500,
    )
    assert restored.effective_for(CAMERA_SCENE_ID) == TransitionSpec(
        TransitionKind.DISSOLVE,
        350,
    )


def test_scene_document_rejects_transition_override_for_missing_scene() -> None:
    document = _document()

    with pytest.raises(SceneValidationError, match="existing scenes"):
        replace(
            document,
            transition_policy=SceneTransitionPolicy(
                default=document.transition_policy.default,
                overrides=(("missing-scene", TransitionSpec(TransitionKind.CUT, 0)),),
            ),
        )


@pytest.mark.parametrize(
    ("legacy_kind", "legacy_duration", "expected"),
    [
        ("cut", 0, TransitionSpec(TransitionKind.CUT, 0)),
        ("fade", 0, TransitionSpec(TransitionKind.DISSOLVE, 50)),
        ("fade", 650, TransitionSpec(TransitionKind.DISSOLVE, 650)),
    ],
)
def test_schema_version_eight_migrates_program_transition_policy(
    legacy_kind: str,
    legacy_duration: int,
    expected: TransitionSpec,
) -> None:
    record = _document().to_record()
    record["schema_version"] = 8
    record.pop("transition_policy")
    for route in record["outputs"]:
        route["transition"] = legacy_kind
        route["transition_duration_ms"] = legacy_duration

    restored = SceneDocument.from_record(record)

    assert restored.transition_policy == SceneTransitionPolicy(default=expected)
    assert all(
        "transition" not in route and "transition_duration_ms" not in route
        for route in restored.to_record()["outputs"]
    )


def test_schema_version_eight_uses_virtual_camera_as_transition_source_of_truth() -> None:
    record = _document().to_record()
    record["schema_version"] = 8
    record.pop("transition_policy")
    for route in record["outputs"]:
        route["transition"] = (
            "fade" if route["bus_id"] == BusId.VIRTUAL_CAMERA.value else "cut"
        )
        route["transition_duration_ms"] = (
            425 if route["bus_id"] == BusId.VIRTUAL_CAMERA.value else 0
        )

    restored = SceneDocument.from_record(record)

    assert restored.transition_policy.default == TransitionSpec(
        TransitionKind.DISSOLVE,
        425,
    )


@pytest.mark.parametrize(
    ("legacy_kind", "legacy_duration"),
    [
        ("unknown", 0),
        ("cut", 1),
        ("fade", -1),
        ("fade", 10_001),
        ("fade", True),
    ],
)
def test_schema_version_eight_rejects_invalid_legacy_transitions(
    legacy_kind: str,
    legacy_duration: object,
) -> None:
    record = _document().to_record()
    record["schema_version"] = 8
    record.pop("transition_policy")
    for route in record["outputs"]:
        route["transition"] = legacy_kind
        route["transition_duration_ms"] = legacy_duration

    with pytest.raises(SceneValidationError, match="(?i)legacy.*transition"):
        SceneDocument.from_record(record)


def test_current_schema_rejects_transition_fields_inside_output_route() -> None:
    record = _document().to_record()
    record["outputs"][0]["transition"] = "cut"

    with pytest.raises(SceneValidationError, match="Unknown output route fields"):
        SceneDocument.from_record(record)


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
    assert restored.schema_version == SCHEMA_VERSION
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


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (10_000_000, 333_333),
        (10_000_000, 166_667),
        (2_147_483_647, 143_165_577),
        (2_147_483_647, 35_791_395),
        (1, 2_147_483_647),
    ],
)
def test_local_camera_preserves_exact_native_fps_components_through_persistence(
    numerator: int, denominator: int
) -> None:
    configuration = LocalCameraConfig(
        width=1920,
        height=1080,
        fps_numerator=numerator,
        fps_denominator=denominator,
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
    )

    assert LocalCameraConfig.from_record(configuration.to_record()) == configuration
    assert configuration.fps_numerator == numerator
    assert configuration.fps_denominator == denominator


def test_graph_signature_ignores_geometry_and_timestamps_but_tracks_structure() -> None:
    document = _document()
    baseline = scene_engine_graph_signature(document)
    scene = document.scenes[0]
    layer = scene.layers[0]

    # A real edit (via update_scene) both moves the layer AND bumps the scene's
    # updated_at. Neither is structural — geometry is applied live and timestamps
    # are metadata — so the signature must be unchanged (else every resize/move
    # would force a full re-hydrate and re-open every source).
    edited_scene = replace(
        scene,
        layers=(replace(layer, rect=NormalizedRect(x=0.3, y=0.3, width=0.4, height=0.4)),
                *scene.layers[1:]),
        updated_at="2099-12-31T23:59:59+00:00",
    )
    edited_document = replace(
        document, scenes=(edited_scene, *document.scenes[1:]),
    )
    assert scene_engine_graph_signature(edited_document) == baseline

    # A structural change (hiding a layer) DOES change the signature → rebuild.
    hidden = replace(layer, visible=not layer.visible)
    hidden_document = replace(
        document,
        scenes=(replace(scene, layers=(hidden, *scene.layers[1:])), *document.scenes[1:]),
    )
    assert scene_engine_graph_signature(hidden_document) != baseline


@pytest.mark.parametrize("mode", list(SolinContentMode))
def test_content_config_round_trips_explicit_mode(mode) -> None:
    config = SolinContentConfig(mode=mode)
    assert config.to_record() == {"mode": mode.value}
    assert SolinContentConfig.from_record(config.to_record()) == config


@pytest.mark.parametrize("record", [{}, {"mode": "idle"}, {"mode": "presentation", "black": True}, None])
def test_content_config_rejects_invalid_or_implicit_modes(record) -> None:
    with pytest.raises(SceneValidationError):
        SolinContentConfig.from_record(record)


def test_beta_conversion_preserves_custom_source_ids_names_and_duplicate_layers() -> None:
    record = _document().to_record()
    record["schema_version"] = 10
    content = record["sources"][0]
    content["configuration"] = {}
    idle = deepcopy(content)
    idle.update(id="custom.idle", type="yeartext", name="My backdrop")
    record["sources"].append(idle)
    layer = deepcopy(record["scenes"][0]["layers"][0])
    layer.update(id="custom.layer", source_id=idle["id"], name="Backdrop", visible=False, locked=True, rotation_degrees=45.0, opacity=0.6, mirror_x=True)
    layer["rect"] = {"x": 0.2, "y": 0.3, "width": 0.5, "height": 0.4}
    layer["crop"] = {"left": 0.1, "top": 0.2, "right": 0.15, "bottom": 0.05}
    record["scenes"][0]["layers"].extend([layer, dict(layer, id="duplicate.layer")])
    for route in record["outputs"]:
        route["default_scene_id"] = CAMERA_SCENE_ID
    original = deepcopy(record)
    restored = SceneDocument.from_record(record)
    expected = deepcopy(record)
    expected["schema_version"] = SCHEMA_VERSION
    for source in expected["sources"]:
        if source["type"] in {"yeartext", "solin_content"}:
            source.update(type="solin_content", configuration={"mode": "presentation"})
    for route in expected["outputs"]:
        route.pop("default_scene_id")
    assert restored.to_record() == expected
    assert record == original
    assert SceneDocument.from_record(restored.to_record()) == restored


@pytest.mark.parametrize("kind", ["yeartext", "idle_screen"])
def test_current_schema_rejects_removed_source_kinds(kind) -> None:
    record = _document().to_record()
    record["sources"][0]["type"] = kind
    with pytest.raises(SceneValidationError):
        SceneDocument.from_record(record)


def test_content_sources_are_optional_and_accept_arbitrary_ids_and_modes() -> None:
    document = _document()
    source = document.sources[0]
    alternate = replace(source, id="custom.content", configuration=SolinContentConfig(SolinContentMode.MEDIA_ONLY))
    changed = replace(document, sources=(*document.sources, alternate))
    assert SceneDocument.from_record(changed.to_record()) == changed
    without = replace(document, sources=document.sources[1:], scenes=tuple(replace(scene, layers=tuple(layer for layer in scene.layers if layer.source_id != source.id)) for scene in document.scenes))
    assert SceneDocument.from_record(without.to_record()) == without


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (2_147_483_648, 2_147_483_647),
        (1, 2_147_483_648),
        (20_000_000, 666_666),
        (10_000_000, 166_666),
        (10_000_000, 0),
        (True, 1),
        (1, True),
    ],
)
def test_local_camera_rejects_unrepresentable_noncanonical_or_over_budget_fps(
    numerator: int, denominator: int
) -> None:
    record = LocalCameraConfig(
        width=1920,
        height=1080,
        fps_numerator=30,
        fps_denominator=1,
        media_type=CameraMediaType.JPEG,
        pixel_format="JPEG",
    ).to_record()
    record.update(fps_numerator=numerator, fps_denominator=denominator)

    with pytest.raises(SceneValidationError):
        LocalCameraConfig.from_record(record)


@pytest.mark.parametrize(
    "config", [LocalCameraConfig(), RtspCameraConfig(uri="rtsp://camera.local/stream")]
)
@pytest.mark.parametrize("flag", [True, False, None, 0, "true"])
def test_current_camera_configs_forbid_keep_active(config, flag) -> None:
    assert not hasattr(config, "keep_active")
    record = config.to_record()
    assert "keep_active" not in record
    record["keep_active"] = flag
    with pytest.raises(SceneValidationError, match="Unknown.*keep_active"):
        type(config).from_record(record)


@pytest.mark.parametrize("kind", [SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA])
@pytest.mark.parametrize("flag", ["absent", True, False])
def test_schema_twelve_removes_legacy_camera_flag_without_mutating_input(kind, flag) -> None:
    record = _document().to_record()
    record["schema_version"] = 12
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    config = (
        LocalCameraConfig()
        if kind is SourceKind.LOCAL_CAMERA
        else RtspCameraConfig(uri="rtsp://camera.local/live")
    )
    camera.update(type=kind.value, configuration=config.to_record())
    if flag != "absent":
        camera["configuration"]["keep_active"] = flag
    original = deepcopy(record)

    restored = SceneDocument.from_record(record)

    assert restored.schema_version == SCHEMA_VERSION
    assert restored.source(DEFAULT_CAMERA_SOURCE_ID).configuration == config
    assert (
        "keep_active" not in restored.source(DEFAULT_CAMERA_SOURCE_ID).to_record()["configuration"]
    )
    assert record == original


@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
def test_legacy_camera_flag_must_be_boolean(flag) -> None:
    record = _document().to_record()
    record["schema_version"] = 12
    camera = next(
        source for source in record["sources"] if source["id"] == DEFAULT_CAMERA_SOURCE_ID
    )
    camera["configuration"]["keep_active"] = flag
    with pytest.raises(SceneValidationError, match="keep_active"):
        SceneDocument.from_record(record)


@pytest.mark.parametrize("kind", [SourceKind.LOCAL_CAMERA, SourceKind.RTSP_CAMERA])
def test_legacy_camera_migration_preserves_connection_and_ptz(kind) -> None:
    document = _document()
    camera = document.source(DEFAULT_CAMERA_SOURCE_ID)
    binding = OnvifPtzBinding(endpoint="https://camera.local/onvif", credential_ref="ptz-secret")
    config = (
        LocalCameraConfig(
            device_id="native-device",
            width=1920,
            height=1080,
            fps_numerator=30000,
            fps_denominator=1001,
            media_type=CameraMediaType.JPEG,
            pixel_format="MJPG",
            ptz_binding=binding,
        )
        if kind is SourceKind.LOCAL_CAMERA
        else RtspCameraConfig(uri="rtsps://camera.local/live", latency_ms=400, ptz_binding=binding)
    )
    camera = replace(
        camera, kind=kind, configuration=config, enabled=False, credential_ref="stream-secret"
    )
    document = replace(
        document,
        sources=tuple(camera if item.id == camera.id else item for item in document.sources),
    )
    record = document.to_record()
    record["schema_version"] = 12
    next(item for item in record["sources"] if item["id"] == camera.id)["configuration"][
        "keep_active"
    ] = False

    assert SceneDocument.from_record(record) == document


@pytest.mark.parametrize("invalid", [{"keep_active": None}, {"unexpected": True}])
def test_schema_five_cannot_hide_invalid_duplicate_camera_data(invalid) -> None:
    record = _document().to_record()
    record["schema_version"] = 5
    camera = next(item for item in record["sources"] if item["id"] == DEFAULT_CAMERA_SOURCE_ID)
    camera["configuration"]["device_id"] = "native-device"
    duplicate = deepcopy(camera)
    duplicate["id"] = "duplicate"
    duplicate["configuration"].update(invalid)
    record["sources"].append(duplicate)
    original = deepcopy(record)

    with pytest.raises(SceneValidationError):
        SceneDocument.from_record(record)
    assert record == original


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize(
    "ids", [("", ""), ("DevicePath", "devicepath"), ("Old:DevicePath", "New:devicepath")]
)
def test_model_preserves_camera_selectors_for_contextual_identity_checks(
    monkeypatch, platform, ids
) -> None:
    monkeypatch.setattr("sys.platform", platform)
    document = _document()
    camera = document.source(DEFAULT_CAMERA_SOURCE_ID)
    first = replace(camera, configuration=LocalCameraConfig(device_id=ids[0]))
    second = replace(camera, id="second-camera", configuration=LocalCameraConfig(device_id=ids[1]))
    sources = tuple(first if item.id == first.id else item for item in document.sources) + (second,)

    assert replace(document, sources=sources).sources == sources


@pytest.mark.parametrize("version", [5, 6, 12, 13])
def test_legacy_windows_friendly_name_aliases_remain_readable_without_merging(monkeypatch, version):
    monkeypatch.setattr("sys.platform", "win32")
    record = _document().to_record()
    record["schema_version"] = version
    camera = next(item for item in record["sources"] if item["id"] == DEFAULT_CAMERA_SOURCE_ID)
    camera["configuration"]["device_id"] = "Old Name:device-path"
    duplicate = deepcopy(camera)
    duplicate["id"] = "second-camera"
    duplicate["configuration"]["device_id"] = "New Name:device-path"
    record["sources"].append(duplicate)
    original = deepcopy(record)

    restored = SceneDocument.from_record(record)

    assert restored.source(camera["id"]).configuration.device_id == "Old Name:device-path"
    assert restored.source(duplicate["id"]).configuration.device_id == "New Name:device-path"
    assert record == original


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize(
    "ids", [("device", "device"), ("Device", "device"), ("Old:Device", "New:device")]
)
def test_schema_five_preserves_incompatible_camera_connections(monkeypatch, platform, ids):
    monkeypatch.setattr("solin.core.media.camera_identity.sys.platform", platform)
    record = _document().to_record()
    record["schema_version"] = 5
    camera = next(item for item in record["sources"] if item["id"] == DEFAULT_CAMERA_SOURCE_ID)
    camera["configuration"]["device_id"] = ids[0]
    duplicate = deepcopy(camera)
    duplicate["id"] = "second-camera"
    duplicate["configuration"]["device_id"] = ids[1]
    duplicate["configuration"]["ptz_binding"] = OnvifPtzBinding(
        endpoint="https://camera.local/onvif"
    ).to_record()
    duplicate["credential_ref"] = "different-secret"
    record["sources"].append(duplicate)
    original = deepcopy(record)

    restored = SceneDocument.from_record(record)

    assert restored.source(camera["id"]).to_record() == camera
    assert restored.source(duplicate["id"]).to_record() == duplicate
    assert record == original


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
    assert restored.schema_version == SCHEMA_VERSION
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
    assert restored.schema_version == SCHEMA_VERSION
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

    assert restored.schema_version == SCHEMA_VERSION
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

    assert all("default_scene_id" not in route.to_record() for route in restored.outputs)
    assert {
        mapping.scene_for(ContentCategory.IMAGE)
        for mapping in restored.automation
    } == {CONTENT_SCENE_ID}


def test_current_schema_rejects_removed_default_route() -> None:
    record = _document().to_record()
    record["outputs"][0]["default_scene_id"] = CONTENT_SCENE_ID
    with pytest.raises(SceneValidationError):
        SceneDocument.from_record(record)


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
            (BusId.EDITOR, CONTENT_SCENE_ID),
        ),
        render_enabled=(
            (BusId.MEDIA_WINDOWS, False),
            (BusId.VIRTUAL_CAMERA, False),
            (BusId.EDITOR, False),
        ),
        output_enabled=(
            (BusId.MEDIA_WINDOWS, False),
            (BusId.VIRTUAL_CAMERA, False),
            (BusId.EDITOR, False),
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
                (BusId.EDITOR, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
                (BusId.EDITOR, False),
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
                (BusId.EDITOR, CONTENT_SCENE_ID),
            ),
            render_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
                (BusId.EDITOR, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
                (BusId.EDITOR, False),
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
                (BusId.EDITOR, False),
            ),
            output_enabled=(
                (BusId.MEDIA_WINDOWS, False),
                (BusId.VIRTUAL_CAMERA, False),
                (BusId.EDITOR, False),
            ),
        )


def test_video_pixel_format_rejects_removed_dynamic_layout() -> None:
    with pytest.raises(ValueError):
        VideoPixelFormat("dynamic")


@pytest.mark.parametrize(
    ("pixel_format", "accepted"),
    [
        (VideoPixelFormat.BGRA, True),
        (VideoPixelFormat.NV12, False),
    ],
)
def test_shared_memory_frame_channel_requires_bgra_pixels(
    pixel_format: VideoPixelFormat,
    accepted: bool,
) -> None:
    arguments = {
        "channel_id": "content-1",
        "generation": 1,
        "producer_kind": FrameProducerKind.SOLIN_OFFSCREEN,
        "transport": FrameChannelTransport.SHARED_MEMORY_BGRA,
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
