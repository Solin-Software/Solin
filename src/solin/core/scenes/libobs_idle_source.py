"""Session-owned, Qt-free idle producer behind one stable private OBS scene."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.core.media.formats import MediaKind, media_kind_from_path
from solin.core.scenes.idle import IdleScreenState
from solin.core.scenes.libobs_idle_video import IdleVideoError, LibobsIdleVideo

log = logging.getLogger(__name__)


class IdleScreenError(RuntimeError):
    """A rejected update, or an unavailable request recovered to its fallback."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(slots=True)
class _Producer:
    source: Any
    key: tuple[object, ...]
    transport: LibobsIdleVideo | None = None
    disconnect: Callable[[], None] | None = None

    def stop_observing(self) -> None:
        # Keep a failed observer alive for a cleanup retry; never release its
        # source while a native callback could still reference it.
        if self.disconnect is not None:
            self.disconnect()
            self.disconnect = None

    def close(self) -> None:
        self.stop_observing()
        if self.transport is not None:
            self.transport.close()
        else:
            self.source.release()


class LibobsIdleSource:
    """Own one idle decoder, shared by every scene referencing ``source``.

    Construction allocates no native resources. ``source`` is a borrowed view
    of a private scene with the global output canvas dimensions. Callers own
    effective consumer demand and suppress transient false pulses on hydrate.

    Normal failures preserve the last committed state and producer. Recovery
    commits the requested state with a best-effort fallback, then raises the
    original error; ``error_code`` remains available for source-health reports.
    A later apply of that same state retries an unavailable producer.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._scene: Any = None
        self._item: Any = None
        self._producer: _Producer | None = None
        self._retired: list[_Producer] = []
        self._state = IdleScreenState()
        self._requested: IdleScreenState | None = None
        self._cancelled_admission: IdleScreenState | None = None
        self._applied = False
        self._error_code = ""
        self._demand = False
        self._live_demand = False
        # Publish both requested flags in one assignment for the native render
        # thread; a commit must never read a partially updated demand pair.
        self._requested_demand = (False, False)
        self._closed = False
        self._generation = 0
        self._sequence = 0
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)
        self._inflight = 0
        self._wake = threading.Event()

    @property
    def source(self) -> Any:
        """Borrowed stable scene source; consumers must not release it."""
        with self._lock:
            self._ensure_scene()
            return self._scene.as_source()

    @property
    def state(self) -> IdleScreenState:
        with self._lock:
            return self._state

    @property
    def error_code(self) -> str:
        with self._lock:
            producer = self._producer
            transport_error = (
                producer.transport.error_code
                if producer is not None and producer.transport is not None else ""
            )
            return self._error_code or transport_error

    @property
    def transport_error_code(self) -> str:
        """Read committed transport health without a render/control lock inversion."""
        producer = self._producer
        error_code = (
            producer.transport.error_code
            if producer is not None and producer.transport is not None else ""
        )
        return error_code if producer is self._producer else ""

    def _ensure_scene(self) -> None:
        if self._closed:
            raise IdleScreenError("closed")
        if self._scene is None:
            try:
                self._scene = self._runtime.ob.Scene.create_private("solin-idle-screen")
                if self._scene is None:
                    raise RuntimeError("Could not create idle scene")
            except Exception as exc:  # noqa: BLE001 - native creation boundary
                raise IdleScreenError("idle_source_unavailable") from exc

    @staticmethod
    def _key(state: IdleScreenState) -> tuple[object, ...]:
        if state.media_path:
            return ("media", state.media_path)
        if state.yeartext_image_path:
            return ("yeartext", state.yeartext_image_path, state.yeartext_revision)
        return ()

    def admit(self, state: IdleScreenState) -> None:
        """Reserve the latest valid choice before dispatching its preparation.

        Admission allocates nothing and leaves committed state and transport
        intact. A higher revision invalidates even a handler that has not yet
        entered ``apply``. Identical admissions do not cancel useful work.
        Callers must validate session, generation and deadline before admission.
        Explicit readmission permits a retry of a cancelled choice; ``apply``
        alone cannot reopen a cancelled admission from an old dispatched handler.
        """
        with self._lock:
            self._admit(state)
            if state == self._cancelled_admission:
                self._cancelled_admission = None
                self._generation += 1

    def _admit(self, state: IdleScreenState) -> None:
        if self._closed:
            raise IdleScreenError("closed")
        previous = self._requested
        if previous is not None:
            if state.revision < previous.revision:
                raise IdleScreenError("stale_revision")
            if state.revision == previous.revision:
                if state != previous:
                    raise IdleScreenError("revision_conflict")
                return
        self._requested = state
        self._cancelled_admission = None
        self._generation += 1

    def apply(
        self,
        state: IdleScreenState,
        deadline_monotonic_ms: int,
        *,
        recover: bool = False,
    ) -> None:
        """Prepare before committing; stale preparations cannot overwrite newer ones."""
        with self._lock:
            if self._closed:
                raise IdleScreenError("closed")
            self._inflight += 1
        try:
            self._apply(state, deadline_monotonic_ms, recover=recover)
        finally:
            with self._settled:
                self._inflight -= 1
                self._settled.notify_all()

    def _apply(
        self,
        state: IdleScreenState,
        deadline_monotonic_ms: int,
        *,
        recover: bool,
    ) -> None:
        with self._lock:
            self._admit(state)
            if self._applied and state == self._state and not self.error_code:
                return
            if state == self._cancelled_admission:
                raise IdleScreenError("stale_revision")
            self._ensure_scene()
            self._generation += 1
            generation = self._generation
            key = self._key(state)
            current_key = self._producer.key if self._producer else ()
            if self._applied and not self.error_code and key == current_key:
                # The fallback is data until it is needed: no second decoder,
                # no PNG reload, and no transport reset for an active video.
                self._check_deadline(deadline_monotonic_ms)
                self._state = state
                return

        candidate: _Producer | None = None
        error: IdleScreenError | None = None
        try:
            self._check_current(generation, deadline_monotonic_ms)
            candidate = self._prepare(state, generation, deadline_monotonic_ms)
        except IdleScreenError as exc:
            if not recover or exc.error_code in ("stale_revision", "closed"):
                raise
            error = exc
            # Recovery uses the same IPC deadline. If preparation exhausted it,
            # transparent is a valid fallback and must not extend the request.
            try:
                fallback = IdleScreenState(
                    revision=state.revision,
                    yeartext_image_path=state.yeartext_image_path,
                    yeartext_revision=state.yeartext_revision,
                )
                candidate = self._prepare(fallback, generation, deadline_monotonic_ms)
            except IdleScreenError as fallback_error:
                if fallback_error.error_code in ("stale_revision", "closed"):
                    raise
                candidate = None

        try:
            with self._lock:
                self._check_current(generation, None if error else deadline_monotonic_ms)
                demand, live = self._requested_demand
                if candidate and candidate.transport is not None:
                    candidate.transport.set_live(live)
                old = self._swap(candidate)
                self._producer = candidate
                candidate = None
                self._state = state
                self._applied = True
                self._error_code = error.error_code if error else ""
                self._demand = demand
                self._live_demand = live
            self._dispose(old)
        except IdleScreenError:
            raise
        except Exception as exc:  # noqa: BLE001 - native commit/transport boundary
            raise IdleScreenError("idle_source_unavailable") from exc
        finally:
            self._dispose(candidate)
        if error is not None:
            raise error

    @staticmethod
    def _check_deadline(deadline_ms: int) -> None:
        if time.monotonic() * 1000 >= deadline_ms:
            raise IdleScreenError("deadline_exceeded")

    def _check_current(self, generation: int, deadline_ms: int | None) -> None:
        with self._lock:
            if self._closed:
                raise IdleScreenError("closed")
            if generation != self._generation:
                raise IdleScreenError("stale_revision")
        if deadline_ms is not None:
            self._check_deadline(deadline_ms)

    def cancel_preparation(self) -> None:
        """Invalidate pending candidates without changing committed presentation.

        Also invalidate the current admission, including a dispatched handler
        that has not entered ``apply``. Already successful duplicates remain
        no-ops; retrying a cancelled preparation requires explicit readmission.
        Use ``admit`` before queuing a newer valid request, so older handlers
        cannot start after cancellation. Workers use cancellation when draining
        on shutdown. The bounded preparation poll observes the new generation in
        at most one 1/120-second wait. Do not signal the close-only wake event:
        leaving it set would make the next candidate spin instead of waiting.
        """
        with self._lock:
            self._generation += 1
            self._cancelled_admission = self._requested

    def _prepare(
        self,
        state: IdleScreenState,
        generation: int,
        deadline_ms: int,
    ) -> _Producer | None:
        path = state.media_path or state.yeartext_image_path
        if not path:
            return None
        try:
            available = Path(path).is_file()
        except OSError as exc:
            raise IdleScreenError("idle_media_unavailable") from exc
        if not available:
            raise IdleScreenError("idle_media_unavailable")
        kind = media_kind_from_path(path) if state.media_path else MediaKind.IMAGE
        if kind not in (MediaKind.IMAGE, MediaKind.VIDEO):
            raise IdleScreenError("idle_media_invalid")
        candidate: _Producer | None = None
        try:
            with self._lock:
                self._check_current(generation, deadline_ms)
                self._sequence += 1
                name = f"solin-idle-producer-{self._sequence}"
            if kind is MediaKind.VIDEO:
                transport = LibobsIdleVideo(self._runtime, name, path)
                # Own even a partially prepared transport. Failed callback
                # cleanup must stay reachable in the retired-owner queue.
                candidate = _Producer(None, self._key(state), transport=transport)
                transport.prepare(
                    deadline_ms, lambda: self._check_current(generation, deadline_ms),
                )
                candidate.source = transport.source
            else:
                source = self._runtime.ob.Source.create_private(
                    "image_source", name, {"file": "", "unload": False},
                )
                if source is None:
                    raise IdleScreenError("idle_source_unavailable")
                candidate = _Producer(source, self._key(state))
                updated = threading.Event()
                candidate.disconnect = self._runtime.observe_source_updates(source, updated.set)
                source.update({"file": path, "unload": False})
                self._wait_image(source, updated, generation, deadline_ms)
            candidate.stop_observing()
            return candidate
        except IdleVideoError as exc:
            self._dispose(candidate)
            raise IdleScreenError(exc.error_code) from exc
        except IdleScreenError:
            self._dispose(candidate)
            raise
        except Exception as exc:  # noqa: BLE001 - native source preparation boundary
            self._dispose(candidate)
            raise IdleScreenError("idle_source_unavailable") from exc
        except BaseException:  # noqa: BLE001 - interruption must retain cleanup ownership
            self._dispose(candidate)
            raise

    def _wait_image(
        self,
        source: Any,
        updated: threading.Event,
        generation: int,
        deadline_ms: int,
    ) -> None:
        while True:
            self._check_current(generation, deadline_ms)
            # A new source starts with no cached dimensions. Native updates may
            # acknowledge before the image tick uploads its texture; wait for both.
            if updated.is_set() and source.width > 0 and source.height > 0:
                return
            self._wait(deadline_ms)

    def _wait(self, deadline_ms: int) -> None:
        self._wake.wait(max(0.0, min(1 / 120, deadline_ms / 1000 - time.monotonic())))

    def _swap(self, producer: _Producer | None) -> _Producer | None:
        old_item = self._item
        new_item = None

        def replace() -> None:
            nonlocal new_item
            try:
                if producer is not None:
                    new_item = self._scene.add(producer.source)
                    new_item.alignment = 5  # OBS_ALIGN_LEFT | OBS_ALIGN_TOP
                    new_item.pos = (0.0, 0.0)
                    new_item.bounds_type = self._runtime.ob.BoundsType.SCALE_INNER
                    new_item.bounds_alignment = 0  # OBS_ALIGN_CENTER
                    new_item.bounds = (
                        float(self._runtime.video.width),
                        float(self._runtime.video.height),
                    )
                if old_item is not None:
                    old_item.remove()
            except Exception:  # noqa: BLE001 - rollback before the native lock opens
                if new_item is not None:
                    new_item.remove()
                raise

        try:
            self._runtime.atomic_scene_update(self._scene, replace)
        except BaseException:  # noqa: BLE001 - release the candidate before propagating failure
            if new_item is not None:
                new_item.release()
            raise
        self._item = new_item
        if old_item is not None:
            old_item.release()
        return self._producer

    def set_demand(self, demanded: bool, *, live: bool = False) -> None:
        """Keep previews at the initial frame; only effective live demand plays.

        The transport is reconciled even without a demand edge, allowing a
        background preparation to satisfy an immediate return to live.
        """
        self._requested_demand = (bool(demanded), bool(demanded and live))
        # Called from the OBS render thread: never wait for a control-thread
        # commit holding a native scene lock. The next render reconciles it.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._closed:
                return
            demanded, live = self._requested_demand
            producer = self._producer
            if producer is not None and producer.transport is not None:
                try:
                    producer.transport.set_live(live)
                except Exception as exc:  # noqa: BLE001 - native transport boundary
                    raise IdleScreenError("idle_source_unavailable") from exc
            self._demand = demanded
            self._live_demand = live
        finally:
            self._lock.release()

    def _dispose(self, producer: _Producer | None) -> None:
        if producer is None:
            return
        try:
            producer.close()
        except Exception:  # noqa: BLE001 - retain callbacks/source until a successful retry
            log.warning("Could not release idle producer", exc_info=True)
            with self._lock:
                self._retired.append(producer)

    def close(self) -> None:
        """Detach scene items before releasing sources; safe to call repeatedly."""
        with self._settled:
            self._closed = True
            self._generation += 1
            self._wake.set()
            # A canceled preparation must disconnect/release before the parent
            # is allowed to tear down the runtime. Waiting releases the lock.
            while self._inflight:
                self._settled.wait()
            if self._scene is not None:
                old = self._swap(None)
                self._producer = None
                self._scene.release()
                self._scene = None
            else:
                old = self._producer
                self._producer = None
            retired, self._retired = self._retired, []
        self._dispose(old)
        for producer in retired:
            self._dispose(producer)


__all__ = ["IdleScreenError", "LibobsIdleSource"]
