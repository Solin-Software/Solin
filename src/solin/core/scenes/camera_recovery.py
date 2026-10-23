"""Bounded physical-device recovery; capture ownership stays in the runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import time

from solin.core.media.camera_identity import local_camera_device_key
from solin.core.media.obs_camera_capture import CameraCaptureState

log = logging.getLogger(__name__)
_CYCLE_SECONDS = 30.0
_RETRY_DELAYS = (1.0, 3.0)


@dataclass(slots=True)
class _Capture:
    device_id: str
    generation: int
    frame_sequence: int
    attempts: int = 0
    retry_at: float | None = None


class LocalCameraRecovery:
    """Control-thread policy with isolated, single-flight metadata work.

    Only a physical topology notification starts a cycle. poll() never waits
    for discovery or frames; the normal engine control loop drives progress.
    Runtime generations fence source updates independently of scene revisions.
    """

    def __init__(
        self, *, discover: Callable[[], Iterable[str]],
        state: Callable[[str], CameraCaptureState | None],
        reconnect: Callable[[str, int], bool], cancel_discovery: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic, executor: Executor | None = None,
    ) -> None:
        self._discover = discover
        self._state = state
        self._reconnect = reconnect
        self._cancel_discovery = cancel_discovery
        self._clock = monotonic
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-recovery")
        self._targets: dict[str, tuple[str, str]] = {}
        self._captures: dict[str, _Capture] = {}
        self._reactivated: dict[str, int] = {}
        self._observed: set[str] = set()
        self._revision = -1
        self._inventory: Future[tuple[str, ...]] | None = None
        self._inventory_revision = -1
        self._present: set[str] | None = None
        self._started = 0.0
        self._scan_attempts = 0
        self._scan_at = 0.0
        self._closed = False

    def set_targets(self, targets: Mapping[str, str]) -> None:
        """Cancel changed/removed configurations without starting another cycle."""
        current = {local_camera_device_key(device): (device, configuration)
                   for device, configuration in targets.items()}
        self._captures = {key: capture for key, capture in self._captures.items()
                          if current.get(key) == self._targets.get(key)}
        self._reactivated = {key: generation for key, generation in self._reactivated.items()
                             if current.get(key) == self._targets.get(key)}
        for key in self._targets.keys() & current.keys():
            if self._targets[key] != current[key]:
                self._observed.add(key)
        self._targets = current
        if self._revision >= 0 and self._clock() - self._started < _CYCLE_SECONDS:
            self._observe_targets()

    def _observe_targets(self) -> None:
        for key, (device, _configuration) in self._targets.items():
            if key in self._observed:
                continue
            state = self._read_state(device)
            if state is not None:
                self._observed.add(key)
                self._captures[key] = _Capture(device, state.generation, state.frame_sequence)

    def changed(self, revision: int) -> bool:
        if self._closed or revision <= self._revision:
            return False
        self._revision = revision
        self._started = self._clock()
        self._captures.clear()
        self._observed.clear()
        self._present = None
        self._scan_attempts = 0
        self._scan_at = self._started
        self._observe_targets()
        return True

    def _read_state(self, device: str) -> CameraCaptureState | None:
        try:
            return self._state(device)
        except Exception:  # noqa: BLE001 - a capture failure must not break engine supervision
            log.warning("Could not read local camera recovery state", exc_info=True)
            return None

    def _inventory_ids(self) -> tuple[str, ...]:
        return tuple(self._discover())

    def poll(self) -> None:
        if self._closed:
            return
        now = self._clock()
        if now - self._started >= _CYCLE_SECONDS:
            if self._captures:
                log.info("Local camera recovery revision=%d result=budget_expired elapsed=%.3fs",
                         self._revision, now - self._started)
            self._captures.clear()
        pending = self._inventory
        if pending is not None and pending.done():
            self._inventory = None
            if self._inventory_revision == self._revision and self._captures:
                try:
                    self._present = {local_camera_device_key(device) for device in pending.result()}
                except Exception:  # noqa: BLE001 - discovery errors never mean device removal
                    log.warning("Local camera recovery inventory failed", exc_info=True)
                self._scan_at = now + _RETRY_DELAYS[min(self._scan_attempts - 1, 1)]
        if not self._captures:
            return
        if self._present is None or any(key not in self._present for key in self._captures):
            if self._inventory is None and self._scan_attempts < 3 and now >= self._scan_at:
                self._scan_attempts += 1
                self._inventory_revision = self._revision
                self._inventory = self._executor.submit(self._inventory_ids)
        if self._present is None:
            return
        for key, capture in tuple(self._captures.items()):
            if self._present is None or key not in self._present:
                continue
            state = self._read_state(capture.device_id)
            if state is None or state.generation != capture.generation:
                self._captures.pop(key, None)
                continue
            if state.status == "ready":
                if state.frame_sequence != capture.frame_sequence:
                    self._captures.pop(key, None)
                    self._reactivated.pop(key, None)
                    if capture.attempts:
                        log.info("Local camera recovery revision=%d result=ready attempts=%d elapsed=%.3fs",
                                 self._revision, capture.attempts, now - self._started)
                # A quick unplug/replug may precede the probe's timeout. Wait
                # for its fresh-frame check instead of interrupting healthy video.
                continue
            if state.error_code not in {"", "camera_no_frames", "camera_update_failed"}:
                self._captures.pop(key, None)
                continue
            if (state.error_code == "camera_update_failed" and not capture.attempts
                    and self._reactivated.get(key) != state.generation):
                self._captures.pop(key, None)
                continue
            if state.status != "failed":
                continue
            if capture.attempts:
                if capture.attempts >= 3:
                    self._captures.pop(key, None)
                    log.info("Local camera recovery revision=%d result=attempts_exhausted elapsed=%.3fs",
                             self._revision, now - self._started)
                    continue
                if capture.retry_at is None:
                    capture.retry_at = now + _RETRY_DELAYS[capture.attempts - 1]
                if now < capture.retry_at:
                    continue
            capture.attempts += 1
            capture.retry_at = None
            started = self._clock()
            result = "requested"
            try:
                applied = self._reconnect(capture.device_id, capture.generation)
            except Exception:  # noqa: BLE001 - failed native updates remain contextual camera errors
                applied = True  # The probe advances its generation before applying settings.
                result = "update_failed"
                log.warning("Local camera reactivation failed", exc_info=True)
            if applied:
                capture.generation += 1
                self._reactivated[key] = capture.generation
            else:
                # Native-managed platforms need no competing reactivation;
                # their ordinary frame probe continues to report recovery.
                self._captures.pop(key, None)
            log.info("Local camera recovery revision=%d attempt=%d result=%s issued=%s elapsed=%.3fs",
                     self._revision, capture.attempts, result, applied, self._clock() - started)

    def recovering(self, device: str) -> bool:
        return local_camera_device_key(device) in self._reactivated

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._captures.clear()
        try:
            if self._inventory is not None and not self._inventory.done():
                self._cancel_discovery()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
