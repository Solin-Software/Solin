from __future__ import annotations

from dataclasses import dataclass
import threading

from solin.core.scenes.model import (
    CameraPreset,
    OnvifPtzBinding,
    PtzProtocol,
)
from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzControlKind,
    PtzControlRequest,
    PtzExecutionError,
    PtzExecutor,
    PtzRecallRequest,
    PtzRecallStatus,
)


@dataclass
class _Adapter:
    protocol: PtzProtocol = PtzProtocol.ONVIF

    def __post_init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        self.calls.append(request.preset.id)
        self.started.set()
        while not self.release.wait(0.01):
            if cancellation.cancelled:
                return


class _FailingAdapter:
    protocol = PtzProtocol.ONVIF

    def recall(
        self,
        request: PtzRecallRequest,
        cancellation: PtzCancellation,
    ) -> None:
        del request, cancellation
        raise PtzExecutionError("ptz_authentication_failed")


class _ControlAdapter:
    protocol = PtzProtocol.ONVIF

    def __init__(self) -> None:
        self.calls: list[PtzControlKind] = []
        self.changed = threading.Event()

    def recall(self, request, cancellation) -> None:
        del request, cancellation

    def control(
        self,
        request: PtzControlRequest,
        cancellation: PtzCancellation,
    ) -> None:
        assert not cancellation.cancelled
        self.calls.append(request.kind)
        self.changed.set()


def _preset(identifier: str, camera_id: str = "camera-one") -> CameraPreset:
    return CameraPreset(
        id=identifier,
        camera_source_id=camera_id,
        name=identifier,
        remote_token="1",
    )


def _binding() -> OnvifPtzBinding:
    return OnvifPtzBinding(endpoint="https://camera.test/onvif/ptz")


def test_executor_serializes_a_camera_and_keeps_only_the_newest_recall() -> None:
    adapter = _Adapter()
    identifiers = iter(("request-1", "request-2", "request-3"))
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=lambda: next(identifiers),
    )
    try:
        first = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-1"),
            timeout_ms=5000,
        )
        assert adapter.started.wait(1)
        second = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-2"),
            timeout_ms=5000,
        )
        third = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-3"),
            timeout_ms=5000,
        )
        assert second.result(timeout=1).status is PtzRecallStatus.CANCELLED
        adapter.release.set()

        assert first.result(timeout=1).status is PtzRecallStatus.CANCELLED
        assert third.result(timeout=1).status is PtzRecallStatus.SUCCEEDED
        assert adapter.calls == ["preset-1", "preset-3"]
    finally:
        adapter.release.set()
        executor.close()


def test_executor_allows_independent_cameras_to_run_concurrently() -> None:
    adapter = _Adapter()
    identifiers = iter(("request-1", "request-2"))
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=lambda: next(identifiers),
        maximum_parallel_cameras=2,
    )
    try:
        first = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-1"),
            timeout_ms=5000,
        )
        second = executor.recall(
            camera_source_id="camera-two",
            binding=_binding(),
            preset=_preset("preset-2", "camera-two"),
            timeout_ms=5000,
        )
        deadline = threading.Event()
        for _attempt in range(100):
            if len(adapter.calls) == 2:
                break
            deadline.wait(0.01)
        assert len(adapter.calls) == 2
        adapter.release.set()
        assert first.result(timeout=1).succeeded
        assert second.result(timeout=1).succeeded
    finally:
        adapter.release.set()
        executor.close()


def test_executor_reports_adapter_failures_without_exposing_exception_text() -> None:
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: _FailingAdapter()},
        request_id_factory=lambda: "request-1",
    )
    try:
        result = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-1"),
            timeout_ms=5000,
        ).result(timeout=1)
    finally:
        executor.close()

    assert result.status is PtzRecallStatus.FAILED
    assert result.error_code == "ptz_authentication_failed"


def test_executor_rejects_new_camera_lanes_at_the_bound() -> None:
    adapter = _Adapter()
    identifiers = iter(("request-1", "request-2"))
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=lambda: next(identifiers),
        maximum_parallel_cameras=1,
        maximum_camera_lanes=1,
    )
    try:
        running = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-1"),
            timeout_ms=5000,
        )
        assert adapter.started.wait(1)
        rejected = executor.recall(
            camera_source_id="camera-two",
            binding=_binding(),
            preset=_preset("preset-2", "camera-two"),
            timeout_ms=5000,
        ).result(timeout=1)
        adapter.release.set()
        assert running.result(timeout=1).succeeded
    finally:
        adapter.release.set()
        executor.close()

    assert rejected.status is PtzRecallStatus.BUSY
    assert rejected.error_code == "ptz_camera_lane_limit"


def test_executor_cancels_one_recall_without_touching_another_camera() -> None:
    adapter = _Adapter()
    identifiers = iter(("request-1", "request-2"))
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=lambda: next(identifiers),
        maximum_parallel_cameras=2,
    )
    try:
        first = executor.recall(
            camera_source_id="camera-one",
            binding=_binding(),
            preset=_preset("preset-1"),
            timeout_ms=5000,
        )
        second = executor.recall(
            camera_source_id="camera-two",
            binding=_binding(),
            preset=_preset("preset-2", "camera-two"),
            timeout_ms=5000,
        )

        executor.cancel(first)

        cancelled = first.result(timeout=1)
        assert cancelled.status is PtzRecallStatus.CANCELLED
        assert cancelled.error_code == "ptz_recall_cancelled"
        assert not second.done()
        adapter.release.set()
        assert second.result(timeout=1).succeeded
    finally:
        adapter.release.set()
        executor.close()


def test_executor_deadman_stops_continuous_movement() -> None:
    adapter = _ControlAdapter()
    request_number = 0

    def request_id() -> str:
        nonlocal request_number
        request_number += 1
        return f"request-{request_number}"

    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=request_id,
    )
    try:
        moved = executor.move(
            camera_source_id="camera-one",
            binding=_binding(),
            pan=-0.5,
            deadman_ms=100,
        ).result(timeout=1)
        assert moved.succeeded
        for _attempt in range(100):
            if PtzControlKind.STOP in adapter.calls:
                break
            adapter.changed.clear()
            adapter.changed.wait(0.01)
        assert adapter.calls[:2] == [PtzControlKind.MOVE, PtzControlKind.STOP]
    finally:
        executor.close()


def test_stale_deadman_callback_cannot_stop_a_renewed_movement() -> None:
    adapter = _ControlAdapter()
    request_number = 0

    def request_id() -> str:
        nonlocal request_number
        request_number += 1
        return f"request-{request_number}"

    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=request_id,
    )
    binding = _binding()
    try:
        executor.move(
            camera_source_id="camera-one",
            binding=binding,
            pan=-0.5,
            deadman_ms=5000,
        ).result(timeout=1)
        stale_timer = executor._deadman_timers["camera-one"]
        executor.move(
            camera_source_id="camera-one",
            binding=binding,
            pan=-0.5,
            deadman_ms=5000,
        ).result(timeout=1)
        current_timer = executor._deadman_timers["camera-one"]

        executor._deadman_stop("camera-one", binding, stale_timer)

        assert executor._deadman_timers["camera-one"] is current_timer
        assert adapter.calls == [PtzControlKind.MOVE, PtzControlKind.MOVE]
    finally:
        executor.close()


def test_executor_shutdown_sends_stop_for_a_moving_camera() -> None:
    adapter = _ControlAdapter()
    identifiers = iter(("move", "shutdown-stop"))
    executor = PtzExecutor(
        {PtzProtocol.ONVIF: adapter},
        request_id_factory=lambda: next(identifiers),
    )
    executor.move(
        camera_source_id="camera-one",
        binding=_binding(),
        tilt=0.5,
        deadman_ms=5000,
    ).result(timeout=1)

    executor.close()

    assert adapter.calls == [PtzControlKind.MOVE, PtzControlKind.STOP]
