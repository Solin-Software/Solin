from __future__ import annotations

import json
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from solin.controllers.program_recording_controller import (
    ProgramRecordingController,
    next_program_recording_path,
    select_default_recording_directory,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import (
    EngineHealthEvent,
    ProgramRecordingEvent,
    SceneEngineAck,
    SceneEngineCapabilities,
    SceneEngineHealth,
    SceneEngineMetrics,
    SceneEngineStatus,
)
from solin.core.scenes.ipc_protocol import PROTOCOL_VERSION
from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document
from solin.core.scenes.recording import (
    AudioDevice,
    AudioDeviceDirection,
    AudioDeviceDiscovery,
    AudioDeviceSelection,
    AudioSelectionMode,
    ProgramRecordingNativeState,
    ProgramRecordingRequest,
    ProgramRecordingStatus,
)
from solin.core.scenes.repository import SceneCollectionCatalogRepository
from solin.core.scenes.workspace import SceneWorkspaceBusyError


def _names() -> SceneSeedNames:
    return SceneSeedNames(
        content_source="Current content",
        default_camera_source="Default camera",
        no_signal_source="No signal background",
        content_scene="Content",
        camera_scene="Camera",
        content_camera_pip_scene="Content and camera",
        no_signal_scene="No signal",
        content_layer="Content",
        camera_layer="Camera",
        background_layer="Background",
    )


def _paths(tmp_path: Path) -> ProfilePaths:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-a",
    )
    paths.ensure_dirs()
    return paths


class _Runtime(QObject):
    engine_capabilities_changed = Signal(object)
    runtime_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.engine_ready = True
        self.native_window_routing_ready = True
        self.program_output_enabled = True
        self.document = create_default_scene_document(
            _names(),
            document_id="recording-document",
            created_at="2026-08-28T12:00:00+00:00",
        )
        self.demands: list[bool] = []

    def set_program_recording_required(self, required: bool) -> None:
        self.demands.append(required)


class _Engine:
    def __init__(self) -> None:
        self.listeners = []
        self.requests: list[tuple[str, object]] = []
        self.health = _health(SceneEngineStatus.READY)
        self.metrics = SceneEngineMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0)

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def emit(self, event) -> None:
        for listener in tuple(self.listeners):
            listener(event)

    def list_audio_devices(self, *, request_id: str, deadline_ms: int):
        del deadline_ms
        self.requests.append(("list_audio_devices", request_id))
        return _completed(
            AudioDeviceDiscovery(
                supported=True,
                ready=True,
                generation=1,
                devices=(
                    AudioDevice(
                        "microphone-1",
                        "Microphone",
                        AudioDeviceDirection.INPUT,
                        True,
                    ),
                ),
            )
        )

    def start_program_recording(
        self,
        recording: ProgramRecordingRequest,
        *,
        request_id: str,
        deadline_ms: int,
    ):
        del deadline_ms
        self.requests.append(("start", recording))
        return _completed(_ack(request_id))

    def set_program_recording_audio(
        self,
        microphone: AudioDeviceSelection,
        system_audio: AudioDeviceSelection,
        *,
        request_id: str,
        deadline_ms: int,
    ):
        del deadline_ms
        self.requests.append(("audio", (microphone, system_audio)))
        return _completed(_ack(request_id))

    def stop_program_recording(self, *, request_id: str, deadline_ms: int):
        del deadline_ms
        self.requests.append(("stop", request_id))
        return _completed(_ack(request_id))


def _health(status: SceneEngineStatus) -> SceneEngineHealth:
    return SceneEngineHealth(
        status=status,
        session_id="session",
        process_generation="generation",
        process_id=1,
        last_heartbeat_monotonic=1.0,
        restart_count=0,
    )


def _ack(request_id: str) -> SceneEngineAck:
    return SceneEngineAck(
        request_id=request_id,
        session_id="session",
        process_generation="generation",
        sequence=0,
        document_revision=0,
        applied=True,
    )


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


@pytest.fixture
def recording_controller_factory(request, scene_workspace_factory, tmp_path):
    def create(*, minimum_free_bytes: int = 0):
        paths = _paths(tmp_path)
        workspace = scene_workspace_factory(paths, seed_names=_names())
        runtime = _Runtime()
        engine = _Engine()
        identities = (f"request-{index}" for index in range(100))
        recordings = tmp_path / "recordings"
        controller = ProgramRecordingController(
            workspace,
            runtime,  # type: ignore[arg-type]
            engine=engine,  # type: ignore[arg-type]
            profile_paths=paths,
            request_id_factory=lambda: next(identities),
            monotonic=lambda: 100.0,
            now=lambda: datetime(2026, 8, 28, 14, 30, 15),
            default_directory_resolver=lambda: recordings,
            minimum_free_bytes=minimum_free_bytes,
        )
        request.addfinalizer(lambda: None if controller._closed else controller.close())
        runtime.engine_capabilities_changed.emit(
            SceneEngineCapabilities(
                protocol_version=PROTOCOL_VERSION,
                process_generation="generation",
                local_cameras=True,
                rtsp_cameras=True,
                hardware_compositing=True,
                virtual_camera=True,
                d3d11_shared_textures=True,
                program_recording=True,
                audio_input_capture=True,
                system_audio_capture=True,
            )
        )
        return controller, workspace, runtime, engine, paths

    return create


def test_controller_starts_and_stops_once_with_one_render_demand(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory()
    observed = []
    controller.state_changed.connect(observed.append)

    controller.start()

    assert controller.state.status is ProgramRecordingStatus.RECORDING
    assert controller.state.started_at_monotonic == 100.0
    assert runtime.demands == [True]
    request = next(value for operation, value in engine.requests if operation == "start")
    assert isinstance(request, ProgramRecordingRequest)
    assert request.path.name == "Solin 2026-08-28 14-30-15.mp4"
    assert paths.scene_recording_journal_file.exists()

    controller.stop()

    assert controller.state.status is ProgramRecordingStatus.IDLE
    assert runtime.demands == [True, False]
    assert not paths.scene_recording_journal_file.exists()
    assert [state.status for state in observed] == [
        ProgramRecordingStatus.STARTING,
        ProgramRecordingStatus.RECORDING,
        ProgramRecordingStatus.STOPPING,
        ProgramRecordingStatus.IDLE,
    ]


def test_controller_rejects_start_when_native_recording_is_unsupported(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory()
    runtime.engine_capabilities_changed.emit(
        SceneEngineCapabilities(
            protocol_version=PROTOCOL_VERSION,
            process_generation="generation",
            local_cameras=True,
            rtsp_cameras=True,
            hardware_compositing=True,
            virtual_camera=True,
            d3d11_shared_textures=True,
            program_recording=False,
            audio_input_capture=True,
            system_audio_capture=True,
        )
    )

    assert controller.start() is None
    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert controller.state.error_code == "program_recording_unsupported"
    assert runtime.demands == []
    assert not any(operation == "start" for operation, _value in engine.requests)
    assert not paths.scene_recording_journal_file.exists()


def test_controller_rejects_video_format_beyond_native_encoder_budget(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory()
    document = runtime.document
    output = document.output(document.outputs[1].bus_id)
    runtime.document = replace(
        document,
        outputs=tuple(
            replace(
                candidate,
                video_format=replace(candidate.video_format, width=7680, height=4320),
            )
            if candidate.bus_id is output.bus_id
            else candidate
            for candidate in document.outputs
        ),
    )

    assert controller.start() is None
    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert controller.state.error_code == "recording_video_format_unsupported"
    assert runtime.demands == []
    assert not any(operation == "start" for operation, _value in engine.requests)
    assert not paths.scene_recording_journal_file.exists()


def test_render_demand_is_rolled_back_when_enabling_program_raises(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory()

    def set_required(required: bool) -> None:
        runtime.demands.append(required)
        if required:
            raise RuntimeError("simulated render reconciliation failure")

    runtime.set_program_recording_required = set_required  # type: ignore[method-assign]

    assert controller.start() is None
    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert controller.state.error_code == "program_render_unavailable"
    assert runtime.demands == [True, False]
    assert not any(operation == "start" for operation, _value in engine.requests)
    assert not paths.scene_recording_journal_file.exists()


def test_audio_changes_persist_and_apply_atomically_while_recording(
    recording_controller_factory,
) -> None:
    controller, workspace, _runtime, engine, _paths = recording_controller_factory()
    controller.start()
    microphone = AudioDeviceSelection(
        AudioSelectionMode.DEVICE,
        device_id="microphone-1",
        display_name="Microphone",
    )

    controller.set_microphone_selection(microphone)

    assert workspace.active_collection.recording.microphone == microphone
    assert controller.state.active_config is not None
    assert controller.state.active_config.microphone == microphone
    operation, applied = engine.requests[-1]
    assert operation == "audio"
    assert applied == (microphone, AudioDeviceSelection())


def test_busy_controller_blocks_scene_profile_lifecycle(recording_controller_factory) -> None:
    controller, workspace, _runtime, _engine, _paths = recording_controller_factory()
    controller.start()

    with pytest.raises(SceneWorkspaceBusyError, match="Stop recording"):
        workspace.create_collection("Auditorium")


def test_native_audio_warning_does_not_interrupt_recording(recording_controller_factory) -> None:
    controller, _workspace, _runtime, engine, _paths = recording_controller_factory()
    controller.start()
    output_path = controller.state.output_path
    assert output_path is not None

    engine.emit(
        ProgramRecordingEvent(
            ProgramRecordingNativeState(
                status=ProgramRecordingStatus.RECORDING,
                path=str(output_path),
                error_code="",
                message="",
                microphone_warning="device_unavailable",
                system_audio_warning="",
                dropped_frames=0,
                duplicated_frames=2,
            )
        )
    )

    assert controller.state.status is ProgramRecordingStatus.RECORDING
    assert controller.state.microphone_warning == "device_unavailable"
    assert controller.state.duplicated_frames == 2


def test_engine_failure_preserves_partial_and_journal_for_next_startup(
    request,
    recording_controller_factory,
    tmp_path: Path,
) -> None:
    controller, workspace, _runtime, engine, paths = recording_controller_factory()
    controller.start()
    final_path = controller.state.output_path
    assert final_path is not None
    staging_path = Path(f"{final_path}.part")
    staging_path.write_bytes(b"fragmented mp4")

    engine.emit(EngineHealthEvent(_health(SceneEngineStatus.FAILED)))

    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert paths.scene_recording_journal_file.exists()
    controller.close()
    replacement_runtime = _Runtime()
    replacement = ProgramRecordingController(
        workspace,
        replacement_runtime,  # type: ignore[arg-type]
        engine=None,
        profile_paths=paths,
        default_directory_resolver=lambda: tmp_path / "recordings",
    )
    request.addfinalizer(replacement.close)
    assert replacement.state.status is ProgramRecordingStatus.FAILED
    assert replacement.state.output_path == staging_path
    assert staging_path.exists()


def test_default_directory_and_filename_collision_are_deterministic(tmp_path: Path) -> None:
    assert select_default_recording_directory(
        movies="relative/movies",
        documents=tmp_path / "Documents",
        home=tmp_path,
    ) == tmp_path / "Documents" / "Solin"
    directory = tmp_path.resolve()
    when = datetime(2026, 8, 28, 14, 30, 15)
    first = next_program_recording_path(directory, when)
    first.write_bytes(b"recording")

    second = next_program_recording_path(directory, when)

    assert second.name == "Solin 2026-08-28 14-30-15 (2).mp4"


def test_low_disk_preflight_fails_before_creating_a_journal(recording_controller_factory) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory(
        minimum_free_bytes=2**63,
    )

    assert controller.start() is None

    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert controller.state.error_code == "recording_path_unavailable"
    assert runtime.demands == []
    assert not any(operation == "start" for operation, _value in engine.requests)
    assert not paths.scene_recording_journal_file.exists()


def test_unwritable_recording_directory_fails_before_native_start(
    recording_controller_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_write(*_args, **_kwargs):
        raise PermissionError("simulated read-only recording directory")

    monkeypatch.setattr(
        "solin.controllers.program_recording_controller.tempfile.NamedTemporaryFile",
        deny_write,
    )
    controller, _workspace, runtime, engine, paths = recording_controller_factory()

    assert controller.start() is None
    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert controller.state.error_code == "recording_path_unavailable"
    assert runtime.demands == []
    assert not any(operation == "start" for operation, _value in engine.requests)
    assert not paths.scene_recording_journal_file.exists()


def test_forced_shutdown_finalizes_before_releasing_the_scene_runtime(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, engine, paths = recording_controller_factory()
    controller.start()
    final_path = controller.state.output_path
    assert final_path is not None
    final_path.write_bytes(b"finalized mp4")

    controller.close()

    assert [operation for operation, _value in engine.requests].count("stop") == 1
    assert runtime.demands == [True, False]
    assert not paths.scene_recording_journal_file.exists()


def test_failed_recording_drops_recovery_journal_after_native_finalization(
    recording_controller_factory,
) -> None:
    controller, _workspace, _runtime, engine, paths = recording_controller_factory()
    controller.start()
    output_path = controller.state.output_path
    assert output_path is not None
    output_path.write_bytes(b"playable incomplete recording")

    engine.emit(
        ProgramRecordingEvent(
            ProgramRecordingNativeState(
                status=ProgramRecordingStatus.FAILED,
                path=str(output_path),
                error_code="recording_video_failed",
                message="Program video failed after MP4 finalization",
                microphone_warning="",
                system_audio_warning="",
                dropped_frames=1,
                duplicated_frames=0,
            )
        )
    )

    assert controller.state.status is ProgramRecordingStatus.FAILED
    assert output_path.exists()
    assert not paths.scene_recording_journal_file.exists()


def test_recording_configuration_is_independent_per_scene_profile(scene_workspace_factory, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    workspace = scene_workspace_factory(paths, seed_names=_names())
    first_id = workspace.active_collection.id
    first_directory = str((tmp_path / "first-recordings").resolve())
    second_directory = str((tmp_path / "second-recordings").resolve())
    workspace.update_active_recording_config(
        replace(workspace.active_collection.recording, output_directory=first_directory)
    )
    second = workspace.create_collection("Auditorium")
    workspace.activate_collection(second.id)
    assert workspace.active_collection.recording.output_directory == ""
    workspace.update_active_recording_config(
        replace(workspace.active_collection.recording, output_directory=second_directory)
    )

    workspace.activate_collection(first_id)

    assert workspace.active_collection.recording.output_directory == first_directory
    reloaded = scene_workspace_factory(paths, seed_names=_names())
    assert reloaded.catalog.collection(first_id).recording.output_directory == first_directory
    assert reloaded.catalog.collection(second.id).recording.output_directory == second_directory


def test_scene_profile_catalog_v1_migrates_to_recording_defaults(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.scene_profiles_catalog_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 7,
                "active_collection_id": "profile-1",
                "pending_collection_id": "",
                "collections": [{"id": "profile-1", "name": "Auditorium"}],
            }
        ),
        encoding="utf-8",
    )

    catalog = SceneCollectionCatalogRepository(paths.scene_profiles_catalog_file).load()

    assert catalog.active.recording.microphone == AudioDeviceSelection()
    assert catalog.active.recording.system_audio == AudioDeviceSelection()
    assert catalog.active.recording.output_directory == ""
    persisted = json.loads(paths.scene_profiles_catalog_file.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["revision"] == 7
    assert persisted["collections"][0]["recording"]["output_directory"] == ""


def test_disabling_the_virtual_camera_stops_and_saves_the_recording(
    recording_controller_factory,
) -> None:
    """Recording captures Program; switching that output off has to finalise it."""
    controller, _workspace, runtime, _engine, paths = recording_controller_factory()
    controller.start()
    assert controller.state.status is ProgramRecordingStatus.RECORDING

    runtime.program_output_enabled = False
    runtime.runtime_changed.emit(None)

    assert controller.state.status is ProgramRecordingStatus.IDLE
    assert runtime.demands == [True, False]
    # A clean stop retires the recovery journal; a stalled one would leave it.
    assert not paths.scene_recording_journal_file.exists()


def test_runtime_changes_leave_a_live_recording_alone_while_the_output_is_on(
    recording_controller_factory,
) -> None:
    controller, _workspace, runtime, _engine, _paths = recording_controller_factory()
    controller.start()

    runtime.runtime_changed.emit(None)

    assert controller.state.status is ProgramRecordingStatus.RECORDING
