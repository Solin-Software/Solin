from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.engine import (
    EngineHealthEvent,
    ProgramRecordingEvent,
    SceneEngine,
    SceneEngineAck,
    SceneEngineCapabilities,
    SceneEngineEvent,
    SceneEngineStatus,
)
from solin.core.scenes.model import BusId, new_identity
from solin.core.scenes.recording import (
    AudioDeviceDiscovery,
    AudioDeviceSelection,
    ProgramRecordingRequest,
    ProgramRecordingState,
    ProgramRecordingStatus,
    SceneRecordingConfig,
)
from solin.core.scenes.workspace import (
    SceneWorkspaceChange,
    SceneWorkspaceChangeKind,
    SceneWorkspaceService,
)
from solin.core.storage.json_repository import JsonFileRepository


_AUDIO_DISCOVERY_DEADLINE_MS = 3000
_START_DEADLINE_MS = 15_000
_AUDIO_UPDATE_DEADLINE_MS = 3000
_STOP_DEADLINE_MS = 15_000
_MINIMUM_FREE_BYTES = 256 * 1024 * 1024
_JOURNAL_SCHEMA_VERSION = 1
log = logging.getLogger(__name__)


class ProgramRecordingError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _RecordingRenderDemand(Protocol):
    @property
    def engine_ready(self) -> bool: ...

    @property
    def native_window_routing_ready(self) -> bool: ...

    @property
    def document(self): ...

    def set_program_recording_required(self, required: bool) -> None: ...


class ProgramRecordingController(QObject):
    """Owns the one authoritative Program-recording state for every UI surface."""

    state_changed = Signal(object)
    audio_devices_changed = Signal(object)
    configuration_changed = Signal(object)
    busy_changed = Signal(bool)
    _async_result = Signal(object)
    _async_engine_event = Signal(object)

    def __init__(
        self,
        workspace: SceneWorkspaceService,
        runtime: SceneRuntimeController,
        *,
        engine: SceneEngine | None,
        profile_paths: ProfilePaths,
        request_id_factory: Callable[[], str] = new_identity,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        default_directory_resolver: Callable[[], Path] | None = None,
        minimum_free_bytes: int = _MINIMUM_FREE_BYTES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
            raise ValueError("Invalid minimum recording free-space budget")
        self._workspace = workspace
        self._runtime: _RecordingRenderDemand = runtime
        self._engine = engine
        self._request_id_factory = request_id_factory
        self._monotonic = monotonic
        self._now = now
        self._default_directory_resolver = (
            default_directory_resolver or default_program_recording_directory
        )
        self._minimum_free_bytes = minimum_free_bytes
        self._journal = _ProgramRecordingJournal(
            profile_paths.scene_recording_journal_file
        )
        self._state = ProgramRecordingState()
        self._audio_devices = _unavailable_audio_devices("engine_not_ready")
        self._capabilities: SceneEngineCapabilities | None = None
        self._process_generation = ""
        self._audio_request_id = ""
        self._audio_future: Future[AudioDeviceDiscovery] | None = None
        self._audio_update_generation = 0
        self._closed = False
        self._async_result.connect(self._consume_async_result)
        self._async_engine_event.connect(self._consume_engine_event)
        self._unsubscribe_workspace = workspace.subscribe(self._on_workspace_changed)
        self._unsubscribe_guard = workspace.register_catalog_mutation_guard(
            self._catalog_mutation_blocker
        )
        self._unsubscribe_engine = (
            engine.subscribe(self._on_engine_event) if engine is not None else None
        )
        runtime.engine_capabilities_changed.connect(self._on_capabilities_changed)
        self._restore_interrupted_recording()

    @property
    def state(self) -> ProgramRecordingState:
        return self._state

    @property
    def audio_devices(self) -> AudioDeviceDiscovery:
        return self._audio_devices

    @property
    def configuration(self) -> SceneRecordingConfig:
        return self._workspace.active_collection.recording

    @property
    def busy(self) -> bool:
        return self._state.busy

    @property
    def supported(self) -> bool:
        return bool(self._capabilities and self._capabilities.program_recording)

    def effective_output_directory(self) -> Path:
        configured = self.configuration.output_directory
        return Path(configured) if configured else self._default_directory_resolver()

    def toggle(self) -> Future[SceneEngineAck] | None:
        if self._state.status in {
            ProgramRecordingStatus.IDLE,
            ProgramRecordingStatus.FAILED,
        }:
            return self.start()
        if self._state.status is ProgramRecordingStatus.RECORDING:
            return self.stop()
        return None

    def start(self) -> Future[SceneEngineAck] | None:
        if self._closed or self.busy:
            return None
        engine = self._engine
        if engine is None or not self._runtime.engine_ready:
            self._fail("engine_not_ready", "The scene engine is not ready to record")
            return None
        if not self._runtime.native_window_routing_ready:
            self._fail("program_not_ready", "Program is not ready to record")
            return None
        if not self.supported:
            self._fail(
                "program_recording_unsupported",
                "Program recording is unavailable in this scene engine",
            )
            return None
        try:
            directory = self.effective_output_directory()
            _prepare_output_directory(
                directory,
                minimum_free_bytes=self._minimum_free_bytes,
            )
            output_path = next_program_recording_path(directory, self._now())
            configuration = self.configuration
            video = self._runtime.document.output(BusId.VIRTUAL_CAMERA).video_format
        except (OSError, ValueError) as exc:
            self._fail("recording_path_unavailable", str(exc))
            return None
        try:
            recording = ProgramRecordingRequest(
                path=output_path,
                width=video.width,
                height=video.height,
                fps_numerator=video.fps_numerator,
                fps_denominator=video.fps_denominator,
                microphone=configuration.microphone,
                system_audio=configuration.system_audio,
            )
        except ValueError as exc:
            self._fail("recording_video_format_unsupported", str(exc))
            return None
        try:
            self._journal.begin(
                collection_id=self._workspace.active_collection.id,
                final_path=output_path,
                started_at=self._now(),
            )
        except OSError as exc:
            self._fail("recording_path_unavailable", str(exc))
            return None

        self._set_state(
            ProgramRecordingState(
                status=ProgramRecordingStatus.STARTING,
                output_path=output_path,
                active_config=configuration,
            )
        )
        try:
            self._runtime.set_program_recording_required(True)
        except Exception:  # noqa: BLE001 - runtime boundary
            self._release_render_demand()
            self._journal.complete(output_path)
            self._set_state(
                ProgramRecordingState(
                    status=ProgramRecordingStatus.FAILED,
                    output_path=output_path,
                    error_code="program_render_unavailable",
                    message="Program could not be enabled for recording",
                )
            )
            return None
        request_id = self._request_id_factory()
        try:
            future = engine.start_program_recording(
                recording,
                request_id=request_id,
                deadline_ms=_START_DEADLINE_MS,
            )
        except Exception as exc:  # noqa: BLE001 - engine adapter boundary
            self._fail("recording_start_failed", str(exc))
            return None
        self._track_future(future, "start", request_id)
        return future

    def stop(self) -> Future[SceneEngineAck] | None:
        if self._closed or self._state.status is not ProgramRecordingStatus.RECORDING:
            return None
        engine = self._engine
        if engine is None:
            self._fail("engine_not_ready", "The scene engine is not ready to stop recording")
            return None
        self._set_state(replace(self._state, status=ProgramRecordingStatus.STOPPING))
        request_id = self._request_id_factory()
        try:
            future = engine.stop_program_recording(
                request_id=request_id,
                deadline_ms=_STOP_DEADLINE_MS,
            )
        except Exception as exc:  # noqa: BLE001 - engine adapter boundary
            self._fail("recording_stop_failed", str(exc))
            return None
        self._track_future(future, "stop", request_id)
        return future

    def refresh_audio_devices(self) -> Future[AudioDeviceDiscovery] | None:
        engine = self._engine
        if self._closed or engine is None or not self._runtime.engine_ready:
            self._set_audio_devices(_unavailable_audio_devices("engine_not_ready"))
            return None
        if self._audio_request_id and self._audio_future is not None:
            return self._audio_future
        request_id = self._request_id_factory()
        self._audio_request_id = request_id
        try:
            future = engine.list_audio_devices(
                request_id=request_id,
                deadline_ms=_AUDIO_DISCOVERY_DEADLINE_MS,
            )
        except Exception:  # noqa: BLE001 - engine adapter boundary
            self._audio_request_id = ""
            self._set_audio_devices(_unavailable_audio_devices("audio_discovery_failed"))
            return None
        self._audio_future = future
        self._track_future(future, "audio_devices", request_id)
        return future

    def set_microphone_selection(self, selection: AudioDeviceSelection) -> None:
        if not isinstance(selection, AudioDeviceSelection):
            raise TypeError("Invalid recording microphone selection")
        self._set_configuration(replace(self.configuration, microphone=selection))

    def set_system_audio_selection(self, selection: AudioDeviceSelection) -> None:
        if not isinstance(selection, AudioDeviceSelection):
            raise TypeError("Invalid recording system-audio selection")
        self._set_configuration(replace(self.configuration, system_audio=selection))

    def set_output_directory(self, directory: str | Path | None) -> None:
        if self.busy:
            raise ProgramRecordingError(
                "recording_busy",
                "The recording folder cannot change while Program is recording",
            )
        canonical = "" if directory is None or str(directory).strip() == "" else str(Path(directory))
        if canonical and not Path(canonical).is_absolute():
            raise ValueError("Recording output directory must be absolute")
        self._set_configuration(replace(self.configuration, output_directory=canonical))

    def close(self) -> None:
        if self._closed:
            return
        self._finalize_for_shutdown()
        self._closed = True
        self._unsubscribe_workspace()
        self._unsubscribe_guard()
        if self._unsubscribe_engine is not None:
            self._unsubscribe_engine()
        self._release_render_demand()

    def _finalize_for_shutdown(self) -> None:
        """Best-effort bounded finalization for forced or non-UI shutdown paths."""

        if not self.busy or self._engine is None or not self._runtime.engine_ready:
            return
        request_id = self._request_id_factory()
        try:
            acknowledgement = self._engine.stop_program_recording(
                request_id=request_id,
                deadline_ms=_STOP_DEADLINE_MS,
            ).result(timeout=(_STOP_DEADLINE_MS / 1000) + 0.5)
        except Exception:  # noqa: BLE001 - shutdown must preserve the journal and continue
            return
        if (
            isinstance(acknowledgement, SceneEngineAck)
            and acknowledgement.applied
            and self._state.output_path is not None
            and self._state.output_path.exists()
        ):
            self._journal.complete(self._state.output_path)

    def _set_configuration(self, configuration: SceneRecordingConfig) -> None:
        previous = self.configuration
        if configuration == previous:
            return
        self._workspace.update_active_recording_config(configuration)
        if self._state.status in {
            ProgramRecordingStatus.STARTING,
            ProgramRecordingStatus.RECORDING,
        } and (
            configuration.microphone != previous.microphone
            or configuration.system_audio != previous.system_audio
        ):
            self._apply_audio_configuration(configuration)

    def _apply_audio_configuration(self, configuration: SceneRecordingConfig) -> None:
        engine = self._engine
        if engine is None or not self._runtime.engine_ready:
            return
        self._audio_update_generation += 1
        generation = self._audio_update_generation
        request_id = self._request_id_factory()
        try:
            future = engine.set_program_recording_audio(
                configuration.microphone,
                configuration.system_audio,
                request_id=request_id,
                deadline_ms=_AUDIO_UPDATE_DEADLINE_MS,
            )
        except Exception:  # noqa: BLE001 - engine adapter boundary
            if self.busy:
                self._set_state(
                    replace(
                        self._state,
                        error_code="audio_update_failed",
                        message="The selected recording audio device could not be applied",
                    )
                )
            return
        self._track_future(
            future,
            "audio_update",
            (generation, request_id, configuration),
        )

    def _catalog_mutation_blocker(self, operation: str) -> str:
        if not self.busy:
            return ""
        return f"Stop recording to {operation} Scene profiles"

    def _on_workspace_changed(self, change: SceneWorkspaceChange) -> None:
        if change.kind in {
            SceneWorkspaceChangeKind.CATALOG,
            SceneWorkspaceChangeKind.ACTIVATED,
        }:
            self.configuration_changed.emit(change.catalog.active.recording)

    def _on_capabilities_changed(self, capabilities: object) -> None:
        if self._closed or not isinstance(capabilities, SceneEngineCapabilities):
            return
        self._capabilities = capabilities
        if capabilities.process_generation != self._process_generation:
            if self.busy and self._process_generation:
                self._fail(
                    "engine_restarted",
                    "The scene engine restarted while Program was recording",
                )
            self._process_generation = capabilities.process_generation
        self.refresh_audio_devices()

    def _on_engine_event(self, event: SceneEngineEvent) -> None:
        if self._closed:
            return
        self._async_engine_event.emit(event)

    def _consume_engine_event(self, event: object) -> None:
        if self._closed:
            return
        if isinstance(event, ProgramRecordingEvent):
            self._apply_native_state(event)
            return
        if not isinstance(event, EngineHealthEvent):
            return
        health = event.health
        if health.status is SceneEngineStatus.READY:
            if health.process_generation != self._process_generation:
                if self.busy and self._process_generation:
                    self._fail(
                        "engine_restarted",
                        "The scene engine restarted while Program was recording",
                    )
                self._process_generation = health.process_generation
            self.refresh_audio_devices()
            return
        if health.status in {
            SceneEngineStatus.STARTING,
            SceneEngineStatus.FAILED,
            SceneEngineStatus.STOPPED,
        }:
            self._audio_request_id = ""
            self._audio_future = None
            self._set_audio_devices(_unavailable_audio_devices("engine_not_ready"))
            if self.busy:
                self._fail(
                    "engine_unavailable",
                    "The scene engine stopped while Program was recording",
                )

    def _apply_native_state(self, event: ProgramRecordingEvent) -> None:
        native = event.state
        path = Path(native.path) if native.path else self._state.output_path
        if (
            self.busy
            and native.path
            and self._state.output_path is not None
            and Path(native.path) != self._state.output_path
        ):
            return
        if native.status is ProgramRecordingStatus.FAILED:
            self._fail(
                native.error_code or "recording_failed",
                native.message or "Program recording failed",
                output_path=path,
                microphone_warning=native.microphone_warning,
                system_audio_warning=native.system_audio_warning,
                dropped_frames=native.dropped_frames,
                duplicated_frames=native.duplicated_frames,
                frame_feed_p95_ns=native.frame_feed_p95_ns,
            )
            return
        if native.status is ProgramRecordingStatus.IDLE:
            if self.busy:
                self._complete_stop(
                    output_path=path,
                    message=native.message,
                    dropped_frames=native.dropped_frames,
                    duplicated_frames=native.duplicated_frames,
                    frame_feed_p95_ns=native.frame_feed_p95_ns,
                )
            return
        if not self.busy:
            return
        started_at = self._state.started_at_monotonic
        if native.status is ProgramRecordingStatus.RECORDING and started_at is None:
            started_at = self._monotonic()
        self._set_state(
            replace(
                self._state,
                status=native.status,
                started_at_monotonic=started_at,
                output_path=path,
                error_code=native.error_code,
                message=native.message,
                microphone_warning=native.microphone_warning,
                system_audio_warning=native.system_audio_warning,
                dropped_frames=native.dropped_frames,
                duplicated_frames=native.duplicated_frames,
                frame_feed_p95_ns=native.frame_feed_p95_ns,
            )
        )

    def _track_future(
        self,
        future: Future[object],
        operation: str,
        context: object,
    ) -> None:
        def completed(done: Future[object]) -> None:
            try:
                result = done.result()
                error: BaseException | None = None
            except BaseException as exc:  # noqa: BLE001 - process boundary
                result = None
                error = exc
            self._async_result.emit((operation, context, result, error))

        future.add_done_callback(completed)

    def _consume_async_result(self, payload: object) -> None:
        if self._closed:
            return
        if (
            not isinstance(payload, tuple)
            or len(payload) != 4
            or not isinstance(payload[0], str)
        ):
            return
        operation, context, result, error = payload
        if isinstance(error, CancelledError):
            return
        if operation == "audio_devices":
            if context != self._audio_request_id:
                return
            self._audio_request_id = ""
            self._audio_future = None
            if error is not None or not isinstance(result, AudioDeviceDiscovery):
                self._set_audio_devices(_unavailable_audio_devices("audio_discovery_failed"))
            else:
                self._set_audio_devices(result)
            return
        if operation == "audio_update":
            self._finish_audio_update(context, result, error)
            return
        if operation not in {"start", "stop"}:
            return
        if error is not None or not isinstance(result, SceneEngineAck) or not result.applied:
            error_code = getattr(error, "error_code", "") or (
                result.error_code if isinstance(result, SceneEngineAck) else ""
            )
            self._fail(
                error_code or f"recording_{operation}_failed",
                f"Could not {operation} Program recording",
            )
            return
        if operation == "start" and self._state.status is ProgramRecordingStatus.STARTING:
            self._set_state(
                replace(
                    self._state,
                    status=ProgramRecordingStatus.RECORDING,
                    started_at_monotonic=self._monotonic(),
                    error_code="",
                    message="",
                )
            )
        elif operation == "stop" and self._state.status is ProgramRecordingStatus.STOPPING:
            self._complete_stop(output_path=self._state.output_path)

    def _finish_audio_update(
        self,
        context: object,
        result: object,
        error: object,
    ) -> None:
        if not isinstance(context, tuple) or len(context) != 3:
            return
        generation, _request_id, configuration = context
        if generation != self._audio_update_generation or not isinstance(
            configuration,
            SceneRecordingConfig,
        ):
            return
        if error is not None or not isinstance(result, SceneEngineAck) or not result.applied:
            if self.busy:
                self._set_state(
                    replace(
                        self._state,
                        error_code="audio_update_failed",
                        message="The selected recording audio device could not be applied",
                    )
                )
            return
        if self.busy:
            self._set_state(
                replace(
                    self._state,
                    active_config=configuration,
                    error_code="",
                    message="",
                )
            )

    def _complete_stop(
        self,
        *,
        output_path: Path | None,
        message: str = "",
        dropped_frames: int | None = None,
        duplicated_frames: int | None = None,
        frame_feed_p95_ns: int | None = None,
    ) -> None:
        if output_path is not None:
            self._journal.complete(output_path)
        self._release_render_demand()
        self._set_state(
            ProgramRecordingState(
                status=ProgramRecordingStatus.IDLE,
                output_path=output_path,
                message=message,
                dropped_frames=(
                    self._state.dropped_frames
                    if dropped_frames is None
                    else dropped_frames
                ),
                duplicated_frames=(
                    self._state.duplicated_frames
                    if duplicated_frames is None
                    else duplicated_frames
                ),
                frame_feed_p95_ns=(
                    self._state.frame_feed_p95_ns
                    if frame_feed_p95_ns is None
                    else frame_feed_p95_ns
                ),
            )
        )

    def _fail(
        self,
        error_code: str,
        message: str,
        *,
        output_path: Path | None = None,
        microphone_warning: str = "",
        system_audio_warning: str = "",
        dropped_frames: int | None = None,
        duplicated_frames: int | None = None,
        frame_feed_p95_ns: int | None = None,
    ) -> None:
        was_busy = self.busy
        if was_busy:
            self._release_render_demand()
        path = output_path or self._state.output_path
        if was_busy and path is not None:
            # A failed native session may still finalize and atomically rename a
            # playable MP4. Recovery entries are useful only while the staging
            # artifact exists; finalized files and empty failures must not leave
            # stale journal state behind.
            if path.exists() or not _staging_path(path).exists():
                self._journal.complete(path)
        self._set_state(
            ProgramRecordingState(
                status=ProgramRecordingStatus.FAILED,
                output_path=path,
                error_code=error_code,
                message=message,
                microphone_warning=microphone_warning,
                system_audio_warning=system_audio_warning,
                dropped_frames=(
                    self._state.dropped_frames
                    if dropped_frames is None
                    else dropped_frames
                ),
                duplicated_frames=(
                    self._state.duplicated_frames
                    if duplicated_frames is None
                    else duplicated_frames
                ),
                frame_feed_p95_ns=(
                    self._state.frame_feed_p95_ns
                    if frame_feed_p95_ns is None
                    else frame_feed_p95_ns
                ),
            )
        )

    def _set_state(self, state: ProgramRecordingState) -> None:
        if state == self._state:
            return
        was_busy = self._state.busy
        self._state = state
        self.state_changed.emit(state)
        if was_busy != state.busy:
            self.busy_changed.emit(state.busy)

    def _set_audio_devices(self, discovery: AudioDeviceDiscovery) -> None:
        if discovery == self._audio_devices:
            return
        self._audio_devices = discovery
        self.audio_devices_changed.emit(discovery)

    def _release_render_demand(self) -> None:
        try:
            self._runtime.set_program_recording_required(False)
        except Exception:  # noqa: BLE001 - teardown must remain fail-safe
            log.warning("Could not release Program recording render demand", exc_info=True)

    def _restore_interrupted_recording(self) -> None:
        pending = self._journal.first_interrupted()
        if pending is None:
            return
        final_path, staging_path = pending
        self._set_state(
            ProgramRecordingState(
                status=ProgramRecordingStatus.FAILED,
                output_path=staging_path,
                error_code="recording_interrupted",
                message="An unfinished recording was preserved",
            )
        )


def default_program_recording_directory() -> Path:
    from PySide6.QtCore import QStandardPaths

    location = QStandardPaths.StandardLocation
    return select_default_recording_directory(
        movies=QStandardPaths.writableLocation(location.MoviesLocation),
        documents=QStandardPaths.writableLocation(location.DocumentsLocation),
        home=str(Path.home()),
    )


def select_default_recording_directory(
    *,
    movies: str | Path | None,
    documents: str | Path | None,
    home: str | Path,
) -> Path:
    for candidate in (movies, documents, home):
        if candidate is not None and str(candidate).strip():
            path = Path(candidate)
            if path.is_absolute():
                return path / "Solin"
    raise ValueError("No writable default recording directory is available")


def next_program_recording_path(directory: Path, when: datetime) -> Path:
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise ValueError("Recording directory must be absolute")
    if not isinstance(when, datetime):
        raise TypeError("Recording timestamp must be a datetime")
    stem = when.strftime("Solin %Y-%m-%d %H-%M-%S")
    for collision in range(10_000):
        suffix = "" if collision == 0 else f" ({collision + 1})"
        candidate = directory / f"{stem}{suffix}.mp4"
        if not candidate.exists() and not _staging_path(candidate).exists():
            return candidate
    raise OSError("Could not allocate a unique recording filename")


def _prepare_output_directory(
    directory: Path,
    *,
    minimum_free_bytes: int,
) -> None:
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise ValueError("Recording directory must be absolute")
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < minimum_free_bytes:
        raise OSError("The recording folder does not have enough free space")
    with tempfile.NamedTemporaryFile(
        prefix=".solin-recording-",
        dir=directory,
        delete=True,
    ):
        pass


def _staging_path(final_path: Path) -> Path:
    return Path(f"{final_path}.part")


def _unavailable_audio_devices(error_code: str) -> AudioDeviceDiscovery:
    return AudioDeviceDiscovery(
        supported=False,
        ready=True,
        generation=0,
        devices=(),
        error_code=error_code,
    )


class _ProgramRecordingJournal:
    def __init__(self, path: Path) -> None:
        self._repository = JsonFileRepository(path)

    def begin(
        self,
        *,
        collection_id: str,
        final_path: Path,
        started_at: datetime,
    ) -> None:
        entries = self._read_entries()
        if entries is None:
            raise OSError("The Program recording recovery journal is unreadable")
        entry = _ProgramRecordingJournalEntry(
            collection_id=collection_id,
            final_path=final_path,
            staging_path=_staging_path(final_path),
            started_at=started_at.astimezone().isoformat(),
        )
        retained = tuple(candidate for candidate in entries if candidate.final_path != final_path)
        self._write((*retained, entry))

    def first_interrupted(self) -> tuple[Path, Path] | None:
        entries = self._read_entries()
        if entries is None:
            return None
        retained: list[_ProgramRecordingJournalEntry] = []
        interrupted: tuple[Path, Path] | None = None
        for entry in entries:
            if entry.final_path.exists() or not entry.staging_path.exists():
                continue
            retained.append(entry)
            if interrupted is None:
                interrupted = (entry.final_path, entry.staging_path)
        if tuple(retained) != entries:
            self._write(tuple(retained))
        return interrupted

    def complete(self, final_path: Path) -> None:
        entries = self._read_entries()
        if entries is None:
            return
        retained = tuple(entry for entry in entries if entry.final_path != final_path)
        if retained != entries:
            self._write(retained)

    def _write(self, entries: tuple[_ProgramRecordingJournalEntry, ...]) -> None:
        if not entries:
            try:
                self._repository.path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._repository.write(
            {
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "recordings": [entry.to_record() for entry in entries],
            },
            indent=2,
            trailing_newline=True,
        )

    def _read_entries(self) -> tuple[_ProgramRecordingJournalEntry, ...] | None:
        if not self._repository.exists():
            return ()
        try:
            raw = self._repository.read()
            if not isinstance(raw, dict) or set(raw) != {"schema_version", "recordings"}:
                return None
            if raw["schema_version"] != _JOURNAL_SCHEMA_VERSION:
                return None
            recordings = raw["recordings"]
            if not isinstance(recordings, list) or len(recordings) > 1024:
                return None
            entries = tuple(
                _ProgramRecordingJournalEntry.from_record(item) for item in recordings
            )
            if len({entry.final_path for entry in entries}) != len(entries):
                return None
            return entries
        except (OSError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class _ProgramRecordingJournalEntry:
    collection_id: str
    final_path: Path
    staging_path: Path
    started_at: str

    def to_record(self) -> dict[str, str]:
        return {
            "collection_id": self.collection_id,
            "final_path": str(self.final_path),
            "staging_path": str(self.staging_path),
            "started_at": self.started_at,
        }

    @classmethod
    def from_record(cls, raw: object) -> _ProgramRecordingJournalEntry:
        if not isinstance(raw, dict) or set(raw) != {
            "collection_id",
            "final_path",
            "staging_path",
            "started_at",
        }:
            raise ValueError("Invalid Program recording journal entry")
        if not all(
            isinstance(raw[field], str)
            for field in ("collection_id", "final_path", "staging_path", "started_at")
        ):
            raise ValueError("Invalid Program recording journal value")
        final_path = Path(raw["final_path"])
        staging_path = Path(raw["staging_path"])
        if (
            not raw["collection_id"]
            or not final_path.is_absolute()
            or staging_path != _staging_path(final_path)
            or final_path.suffix.casefold() != ".mp4"
        ):
            raise ValueError("Invalid Program recording journal path")
        datetime.fromisoformat(raw["started_at"])
        return cls(
            collection_id=raw["collection_id"],
            final_path=final_path,
            staging_path=staging_path,
            started_at=raw["started_at"],
        )
