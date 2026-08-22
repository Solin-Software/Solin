"""Bounded asynchronous PTZ execution independent from scene rendering."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import overload, Protocol

from solin.core.scenes.model import (
    CameraPreset,
    OnvifPtzBinding,
    PtzBinding,
    PtzProtocol,
    ViscaIpPtzBinding,
    ViscaSerialPtzBinding,
)


class PtzRecallStatus(str, Enum):
    SUCCEEDED = "succeeded"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BUSY = "busy"


class PtzControlKind(str, Enum):
    MOVE = "move"
    STOP = "stop"
    STORE_PRESET = "store_preset"


@dataclass(frozen=True, slots=True)
class PtzRecallRequest:
    request_id: str
    camera_source_id: str
    binding: PtzBinding
    preset: CameraPreset
    deadline_monotonic: float

    def __post_init__(self) -> None:
        if not self.request_id or not self.camera_source_id:
            raise ValueError("PTZ request identities must not be empty")
        if self.preset.camera_source_id != self.camera_source_id:
            raise ValueError("PTZ preset belongs to a different camera")
        if not isinstance(
            self.binding,
            (OnvifPtzBinding, ViscaIpPtzBinding, ViscaSerialPtzBinding),
        ):
            raise TypeError("PTZ request binding is invalid")
        if not isinstance(self.deadline_monotonic, float):
            raise TypeError("PTZ deadline must use the monotonic clock")

    def remaining_seconds(self, clock: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, self.deadline_monotonic - clock())


@dataclass(frozen=True, slots=True)
class PtzRecallResult:
    request_id: str
    camera_source_id: str
    preset_id: str
    status: PtzRecallStatus
    error_code: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is PtzRecallStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class PtzControlRequest:
    request_id: str
    camera_source_id: str
    binding: PtzBinding
    kind: PtzControlKind
    deadline_monotonic: float
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    preset: CameraPreset | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not self.camera_source_id:
            raise ValueError("PTZ request identities must not be empty")
        if not isinstance(
            self.binding,
            (OnvifPtzBinding, ViscaIpPtzBinding, ViscaSerialPtzBinding),
        ):
            raise TypeError("PTZ request binding is invalid")
        if not isinstance(self.kind, PtzControlKind):
            raise TypeError("PTZ control kind is invalid")
        if not isinstance(self.deadline_monotonic, float):
            raise TypeError("PTZ deadline must use the monotonic clock")
        values = (self.pan, self.tilt, self.zoom)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("PTZ velocity must be numeric")
        if any(not -1.0 <= float(value) <= 1.0 for value in values):
            raise ValueError("PTZ velocity must be normalized")
        if self.kind is PtzControlKind.MOVE and not any(values):
            raise ValueError("PTZ move requires pan, tilt, or zoom velocity")
        if self.kind is not PtzControlKind.MOVE and any(values):
            raise ValueError("Only PTZ move accepts velocity")
        if self.kind is PtzControlKind.STORE_PRESET:
            if self.preset is None or self.preset.camera_source_id != self.camera_source_id:
                raise ValueError("PTZ store request requires a preset for the camera")
        elif self.preset is not None:
            raise ValueError("PTZ preset is accepted only by store commands")

    def remaining_seconds(self, clock: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, self.deadline_monotonic - clock())


@dataclass(frozen=True, slots=True)
class PtzControlResult:
    request_id: str
    camera_source_id: str
    kind: PtzControlKind
    status: PtzRecallStatus
    error_code: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is PtzRecallStatus.SUCCEEDED


class PtzExecutionError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        if not error_code or len(error_code) > 128:
            raise ValueError("PTZ error code is invalid")
        super().__init__(error_code)
        self.error_code = error_code


class PtzCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PtzCredentials:
    username: str
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.username
            or len(self.username) > 256
            or not self.password
            or len(self.password) > 1024
            or any(ord(character) < 32 for character in self.username)
        ):
            raise ValueError("PTZ credentials are invalid")


class PtzCredentialResolver(Protocol):
    def resolve(self, credential_ref: str) -> PtzCredentials | None: ...


class PtzCredentialError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        if not error_code or len(error_code) > 128:
            raise ValueError("PTZ credential error code is invalid")
        super().__init__(error_code)
        self.error_code = error_code


class PtzCredentialVault(PtzCredentialResolver, Protocol):
    def save(self, credentials: PtzCredentials) -> str: ...

    def delete(self, credential_ref: str) -> None: ...


class PtzCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(max(0.0, timeout))


class PtzAdapter(Protocol):
    @property
    def protocol(self) -> PtzProtocol: ...

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        """Return only after positioning completes or raise a bounded failure."""

    def control(
        self,
        request: PtzControlRequest,
        cancellation: PtzCancellation,
    ) -> None: ...


class PtzRecallExecutor(Protocol):
    def recall(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        preset: CameraPreset,
        timeout_ms: int,
    ) -> Future[PtzRecallResult]: ...

    def move(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        pan: float = 0.0,
        tilt: float = 0.0,
        zoom: float = 0.0,
        timeout_ms: int = 1500,
        deadman_ms: int = 750,
    ) -> Future[PtzControlResult]: ...

    def stop(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        timeout_ms: int = 1500,
    ) -> Future[PtzControlResult]: ...

    def store_preset(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        preset: CameraPreset,
        timeout_ms: int = 3000,
    ) -> Future[PtzControlResult]: ...

    def cancel(
        self,
        future: Future[PtzRecallResult] | Future[PtzControlResult],
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _RecallWorkItem:
    request: PtzRecallRequest
    cancellation: PtzCancellation
    future: Future[PtzRecallResult]
    cancellation_error_code: str


@dataclass(slots=True)
class _ControlWorkItem:
    request: PtzControlRequest
    cancellation: PtzCancellation
    future: Future[PtzControlResult]
    cancellation_error_code: str


type _WorkItem = _RecallWorkItem | _ControlWorkItem


@dataclass(slots=True)
class _CameraLane:
    running: _WorkItem
    pending: _WorkItem | None = None


class PtzExecutor:
    """Runs at most one command per camera and keeps only its newest queued recall."""

    def __init__(
        self,
        adapters: Mapping[PtzProtocol, PtzAdapter],
        *,
        request_id_factory: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
        maximum_parallel_cameras: int = 4,
        maximum_camera_lanes: int = 16,
    ) -> None:
        if set(adapters) != {adapter.protocol for adapter in adapters.values()}:
            raise ValueError("PTZ adapter registry keys do not match their protocols")
        if maximum_parallel_cameras <= 0 or maximum_camera_lanes <= 0:
            raise ValueError("PTZ executor limits must be positive")
        if maximum_parallel_cameras > maximum_camera_lanes:
            raise ValueError("PTZ worker count cannot exceed the camera lane limit")
        self._adapters = dict(adapters)
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._maximum_camera_lanes = maximum_camera_lanes
        self._lock = threading.Lock()
        self._lanes: dict[str, _CameraLane] = {}
        self._deadman_timers: dict[str, threading.Timer] = {}
        self._moving_bindings: dict[str, PtzBinding] = {}
        self._closed = False
        self._workers = ThreadPoolExecutor(
            max_workers=maximum_parallel_cameras,
            thread_name_prefix="solin-ptz",
        )

    def recall(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        preset: CameraPreset,
        timeout_ms: int,
    ) -> Future[PtzRecallResult]:
        if not 1 <= timeout_ms <= 60_000:
            raise ValueError("PTZ timeout must be between 1 and 60000 ms")
        request = PtzRecallRequest(
            request_id=self._request_id_factory(),
            camera_source_id=camera_source_id,
            binding=binding,
            preset=preset,
            deadline_monotonic=self._clock() + timeout_ms / 1000.0,
        )
        self._cancel_deadman(camera_source_id)
        item = _RecallWorkItem(
            request,
            PtzCancellation(),
            Future(),
            "ptz_recall_superseded",
        )
        return self._enqueue(item)

    def move(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        pan: float = 0.0,
        tilt: float = 0.0,
        zoom: float = 0.0,
        timeout_ms: int = 1500,
        deadman_ms: int = 750,
    ) -> Future[PtzControlResult]:
        if not 100 <= deadman_ms <= 5000:
            raise ValueError("PTZ dead-man timeout must be between 100 and 5000 ms")
        request = self._control_request(
            camera_source_id=camera_source_id,
            binding=binding,
            kind=PtzControlKind.MOVE,
            timeout_ms=timeout_ms,
            pan=pan,
            tilt=tilt,
            zoom=zoom,
        )
        with self._lock:
            if not self._closed:
                self._moving_bindings[camera_source_id] = binding
        future = self._enqueue(
            _ControlWorkItem(
                request,
                PtzCancellation(),
                Future(),
                "ptz_command_superseded",
            )
        )
        self._arm_deadman(camera_source_id, binding, deadman_ms)
        return future

    def stop(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        timeout_ms: int = 1500,
    ) -> Future[PtzControlResult]:
        self._cancel_deadman(camera_source_id)
        request = self._control_request(
            camera_source_id=camera_source_id,
            binding=binding,
            kind=PtzControlKind.STOP,
            timeout_ms=timeout_ms,
        )
        return self._enqueue(
            _ControlWorkItem(
                request,
                PtzCancellation(),
                Future(),
                "ptz_command_superseded",
            )
        )

    def store_preset(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        preset: CameraPreset,
        timeout_ms: int = 3000,
    ) -> Future[PtzControlResult]:
        request = self._control_request(
            camera_source_id=camera_source_id,
            binding=binding,
            kind=PtzControlKind.STORE_PRESET,
            timeout_ms=timeout_ms,
            preset=preset,
        )
        return self._enqueue(
            _ControlWorkItem(
                request,
                PtzCancellation(),
                Future(),
                "ptz_command_superseded",
            )
        )

    @overload
    def _enqueue(self, item: _RecallWorkItem) -> Future[PtzRecallResult]: ...

    @overload
    def _enqueue(self, item: _ControlWorkItem) -> Future[PtzControlResult]: ...

    def _enqueue(
        self,
        item: _WorkItem,
    ) -> Future[PtzRecallResult] | Future[PtzControlResult]:
        camera_source_id = item.request.camera_source_id
        submit = False
        with self._lock:
            if self._closed:
                _resolve(item, PtzRecallStatus.CANCELLED, "ptz_executor_closed")
                return item.future
            lane = self._lanes.get(camera_source_id)
            if lane is None:
                if len(self._lanes) >= self._maximum_camera_lanes:
                    _resolve(item, PtzRecallStatus.BUSY, "ptz_camera_lane_limit")
                    return item.future
                self._lanes[camera_source_id] = _CameraLane(running=item)
                submit = True
            else:
                _cancel(lane.running, item.cancellation_error_code)
                if lane.pending is not None:
                    _cancel(lane.pending, item.cancellation_error_code)
                    _resolve(
                        lane.pending,
                        PtzRecallStatus.CANCELLED,
                        item.cancellation_error_code,
                    )
                lane.pending = item
        if submit:
            self._submit(camera_source_id, item)
        return item.future

    def cancel(
        self,
        future: Future[PtzRecallResult] | Future[PtzControlResult],
    ) -> None:
        with self._lock:
            for lane in self._lanes.values():
                if lane.running.future is future:
                    _cancel(lane.running, "ptz_recall_cancelled")
                    return
                if lane.pending is not None and lane.pending.future is future:
                    pending = lane.pending
                    lane.pending = None
                    _cancel(pending, "ptz_recall_cancelled")
                    _resolve(
                        pending,
                        PtzRecallStatus.CANCELLED,
                        pending.cancellation_error_code,
                    )
                    return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            moving = tuple(self._moving_bindings.items())
            self._moving_bindings.clear()
            timers = tuple(self._deadman_timers.values())
            self._deadman_timers.clear()
            lanes = tuple(self._lanes.values())
            for lane in lanes:
                _cancel(lane.running, "ptz_executor_closed")
                if lane.pending is not None:
                    _cancel(lane.pending, "ptz_executor_closed")
                    _resolve(
                        lane.pending,
                        PtzRecallStatus.CANCELLED,
                        "ptz_executor_closed",
                    )
                    lane.pending = None
        for timer in timers:
            timer.cancel()
        self._send_shutdown_stops(moving)
        self._workers.shutdown(wait=False, cancel_futures=False)

    def _execute(self, camera_source_id: str, item: _WorkItem) -> None:
        request = item.request
        status = PtzRecallStatus.FAILED
        recall = isinstance(request, PtzRecallRequest)
        failed_error_code = "ptz_recall_failed" if recall else "ptz_control_failed"
        timeout_error_code = "ptz_recall_timeout" if recall else "ptz_control_timeout"
        error_code = failed_error_code
        try:
            if item.cancellation.cancelled:
                status = PtzRecallStatus.CANCELLED
                error_code = item.cancellation_error_code
            elif request.remaining_seconds(self._clock) <= 0:
                status = PtzRecallStatus.TIMED_OUT
                error_code = timeout_error_code
            else:
                adapter = self._adapters.get(request.binding.protocol)
                if adapter is None:
                    raise PtzExecutionError("ptz_protocol_unavailable")
                if isinstance(request, PtzRecallRequest):
                    adapter.recall(request, item.cancellation)
                else:
                    control = getattr(adapter, "control", None)
                    if not callable(control):
                        raise PtzExecutionError("ptz_control_unsupported")
                    control(request, item.cancellation)
                if item.cancellation.cancelled:
                    status = PtzRecallStatus.CANCELLED
                    error_code = item.cancellation_error_code
                elif request.remaining_seconds(self._clock) <= 0:
                    status = PtzRecallStatus.TIMED_OUT
                    error_code = timeout_error_code
                else:
                    status = PtzRecallStatus.SUCCEEDED
                    error_code = ""
        except TimeoutError:
            status = PtzRecallStatus.TIMED_OUT
            error_code = timeout_error_code
        except PtzCancelledError:
            status = PtzRecallStatus.CANCELLED
            error_code = item.cancellation_error_code
        except PtzExecutionError as error:
            status = PtzRecallStatus.FAILED
            error_code = error.error_code
        except (OSError, RuntimeError, ValueError):
            status = PtzRecallStatus.FAILED
            error_code = failed_error_code
        _resolve(item, status, error_code)

        if (
            isinstance(request, PtzControlRequest)
            and request.kind is PtzControlKind.STOP
            and status is PtzRecallStatus.SUCCEEDED
        ):
            with self._lock:
                self._moving_bindings.pop(camera_source_id, None)

        next_item: _WorkItem | None = None
        with self._lock:
            lane = self._lanes.get(camera_source_id)
            if lane is None or lane.running is not item:
                return
            next_item = lane.pending
            if next_item is None or self._closed:
                self._lanes.pop(camera_source_id, None)
            else:
                lane.running = next_item
                lane.pending = None
        if next_item is not None and not self._closed:
            self._submit(camera_source_id, next_item)

    def _submit(self, camera_source_id: str, item: _WorkItem) -> None:
        try:
            self._workers.submit(self._execute, camera_source_id, item)
        except RuntimeError:
            with self._lock:
                lane = self._lanes.get(camera_source_id)
                if lane is not None and lane.running is item:
                    self._lanes.pop(camera_source_id, None)
            _cancel(item, "ptz_executor_closed")
            _resolve(item, PtzRecallStatus.CANCELLED, "ptz_executor_closed")

    def _control_request(
        self,
        *,
        camera_source_id: str,
        binding: PtzBinding,
        kind: PtzControlKind,
        timeout_ms: int,
        pan: float = 0.0,
        tilt: float = 0.0,
        zoom: float = 0.0,
        preset: CameraPreset | None = None,
    ) -> PtzControlRequest:
        if not 100 <= timeout_ms <= 60_000:
            raise ValueError("PTZ timeout must be between 100 and 60000 ms")
        return PtzControlRequest(
            request_id=self._request_id_factory(),
            camera_source_id=camera_source_id,
            binding=binding,
            kind=kind,
            deadline_monotonic=self._clock() + timeout_ms / 1000.0,
            pan=pan,
            tilt=tilt,
            zoom=zoom,
            preset=preset,
        )

    def _arm_deadman(
        self,
        camera_source_id: str,
        binding: PtzBinding,
        deadman_ms: int,
    ) -> None:
        def expire() -> None:
            self._deadman_stop(camera_source_id, binding, timer)

        timer = threading.Timer(
            deadman_ms / 1000.0,
            expire,
        )
        timer.daemon = True
        with self._lock:
            if self._closed:
                return
            previous = self._deadman_timers.get(camera_source_id)
            self._deadman_timers[camera_source_id] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _cancel_deadman(self, camera_source_id: str) -> None:
        with self._lock:
            timer = self._deadman_timers.pop(camera_source_id, None)
        if timer is not None:
            timer.cancel()

    def _deadman_stop(
        self,
        camera_source_id: str,
        binding: PtzBinding,
        timer: threading.Timer,
    ) -> None:
        with self._lock:
            current = self._deadman_timers.get(camera_source_id)
            if current is not timer:
                return
            self._deadman_timers.pop(camera_source_id, None)
            closed = self._closed
        timer.cancel()
        if not closed:
            self.stop(
                camera_source_id=camera_source_id,
                binding=binding,
                timeout_ms=1000,
            )

    def _send_shutdown_stops(
        self,
        moving: tuple[tuple[str, PtzBinding], ...],
    ) -> None:
        if not moving:
            return
        workers = ThreadPoolExecutor(
            max_workers=min(4, len(moving)),
            thread_name_prefix="solin-ptz-stop",
        )
        futures = []
        for camera_source_id, binding in moving:
            adapter = self._adapters.get(binding.protocol)
            control = getattr(adapter, "control", None)
            if not callable(control):
                continue
            request = PtzControlRequest(
                request_id=self._request_id_factory(),
                camera_source_id=camera_source_id,
                binding=binding,
                kind=PtzControlKind.STOP,
                deadline_monotonic=self._clock() + 0.75,
            )
            futures.append(workers.submit(control, request, PtzCancellation()))
        if futures:
            wait(futures, timeout=1.0)
        workers.shutdown(wait=False, cancel_futures=True)


@overload
def _result(
    item: _RecallWorkItem,
    status: PtzRecallStatus,
    error_code: str,
) -> PtzRecallResult: ...


@overload
def _result(
    item: _ControlWorkItem,
    status: PtzRecallStatus,
    error_code: str,
) -> PtzControlResult: ...


def _result(
    item: _WorkItem,
    status: PtzRecallStatus,
    error_code: str,
) -> PtzRecallResult | PtzControlResult:
    if isinstance(item, _RecallWorkItem):
        request = item.request
        return PtzRecallResult(
            request_id=request.request_id,
            camera_source_id=request.camera_source_id,
            preset_id=request.preset.id,
            status=status,
            error_code=error_code,
        )
    request = item.request
    return PtzControlResult(
        request_id=request.request_id,
        camera_source_id=request.camera_source_id,
        kind=request.kind,
        status=status,
        error_code=error_code,
    )


def _resolve(
    item: _WorkItem,
    status: PtzRecallStatus,
    error_code: str,
) -> None:
    if isinstance(item, _RecallWorkItem):
        if not item.future.done():
            item.future.set_result(_result(item, status, error_code))
    elif not item.future.done():
        item.future.set_result(_result(item, status, error_code))


def _cancel(item: _WorkItem, error_code: str) -> None:
    item.cancellation_error_code = error_code
    item.cancellation.cancel()
