"""Serialize monitoring work with native OBS source destruction."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import logging
import sys
import threading
from typing import Any, TypeVar


log = logging.getLogger(__name__)
_T = TypeVar("_T")


def _retain_mta() -> Callable[[], None]:
    """Keep the native lane's implicit MTA alive through OBS shutdown."""
    import ctypes

    if sys.platform != "win32":
        raise RuntimeError("The OBS task lane requires Windows COM")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoIncrementMTAUsage.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
    ole32.CoIncrementMTAUsage.restype = ctypes.c_int32
    ole32.CoDecrementMTAUsage.argtypes = (ctypes.c_void_p,)
    ole32.CoDecrementMTAUsage.restype = ctypes.c_int32
    cookie = ctypes.c_void_p()
    result = ole32.CoIncrementMTAUsage(ctypes.byref(cookie))
    if result < 0:
        raise OSError(f"OBS task MTA acquisition failed: 0x{result & 0xFFFFFFFF:08X}")

    def release() -> None:
        result = ole32.CoDecrementMTAUsage(cookie)
        if result < 0:
            raise OSError(f"OBS task MTA release failed: 0x{result & 0xFFFFFFFF:08X}")

    return release


class _NativeTaskBridge:
    """Own the application callbacks submitted to the native destruction lane."""

    def __init__(self, dispatch: Callable[[Callable[[], None], bool], None]) -> None:
        from pylibobs._ffi import ffi, get_lib

        self._lib: Any = get_lib()
        self._ffi: Any = ffi
        self._handles: dict[int, Any] = {}
        self._lock = threading.Lock()

        @ffi.callback("obs_task_handler_t")
        def handler(task: Any, param: Any, wait: bool) -> None:
            dispatch(lambda: task(param), wait)

        self._callback: Any = handler

        @ffi.callback("obs_task_t")
        def execute(param: Any) -> None:
            try:
                ffi.from_handle(param)()
            except BaseException:  # noqa: BLE001 - native queue must signal its waiter
                log.exception("OBS task trampoline failed")
            finally:
                with self._lock:
                    self._handles.pop(int(ffi.cast("uintptr_t", param)), None)

        self._task_callback: Any = execute

    def install(self) -> None:
        """Register after startup; retain this owner until native shutdown ends."""
        self._lib.obs_set_ui_task_handler(self._callback)

    def inside(self) -> bool:
        return bool(self._lib.obs_in_task_thread(3))  # OBS_TASK_DESTROY

    def queue(self, callback: Callable[[], None], *, wait: bool) -> None:
        handle = self._ffi.new_handle(callback)
        key = int(self._ffi.cast("uintptr_t", handle))
        with self._lock:
            self._handles[key] = handle
        try:
            self._lib.obs_queue_task(3, self._task_callback, handle, wait)
        except BaseException:  # noqa: BLE001 - release the retained handle before propagating enqueue failure
            with self._lock:
                self._handles.pop(key, None)
            raise


class ObsTaskDispatcher:
    """Route UI tasks and monitoring changes onto OBS_TASK_DESTROY.

    The lifecycle owner installs after startup, quiesces before native shutdown,
    and closes after shutdown. Native submissions may arrive from any thread.
    Lifecycle calls must be serialized and run outside the destruction lane.
    The MTA guard outlives quiescence to support final native source destruction.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending = 0
        self._accepting = False
        self._quiesced = False
        self._closed = False
        self._bridge: _NativeTaskBridge | None = None
        self._release_mta: Callable[[], None] | None = None

    def install(self) -> None:
        """Retain MTA support and register the UI handler before module loading."""
        if self._quiesced or self._closed:
            raise RuntimeError("OBS task dispatcher cannot be reinstalled after quiescence")
        if self._bridge is not None:
            return
        try:
            self._release_mta = _retain_mta()
            self._bridge = _NativeTaskBridge(self._dispatch)
            with self._condition:
                self._accepting = True
            self._bridge.install()
            # Registration queues OBS' thread-local UI marker on this same lane.
            self.run(lambda: None)
        except BaseException:  # noqa: BLE001 - quiesce partial registration before propagating failure
            self.quiesce()
            raise

    def run(self, callback: Callable[[], _T]) -> _T:
        """Run monitoring work synchronously, returning its result or exception."""
        result = self._submit(callback, wait=True, native=False)
        if result is None:
            raise RuntimeError("OBS task dispatcher is not accepting work")
        return result.result()

    def quiesce(self) -> None:
        """Reject and drain work, retaining the MTA guard and inert UI handler."""
        if self._quiesced:
            return
        bridge = self._bridge
        if bridge is not None and bridge.inside():
            raise RuntimeError("OBS task lifecycle cannot run on its destruction lane")
        with self._condition:
            self._accepting = False
            self._condition.wait_for(lambda: self._pending == 0)
        self._quiesced = True

    def close(self) -> None:
        """Release MTA and callback ownership after shutdown; call no OBS API."""
        if self._bridge is not None and not self._quiesced:
            raise RuntimeError("Quiesce OBS tasks before native shutdown and close")
        if self._release_mta is not None:
            self._release_mta()
            self._release_mta = None
        self._bridge = None
        self._closed = True

    def _dispatch(self, callback: Callable[[], None], wait: bool) -> None:
        try:
            self._submit(callback, wait=wait, native=True)
        except BaseException:  # noqa: BLE001 - never unwind through a native callback
            log.exception("Could not dispatch OBS UI task")

    def _submit(
        self,
        callback: Callable[[], _T],
        *,
        wait: bool,
        native: bool,
    ) -> Future[_T] | None:
        with self._condition:
            if not self._accepting:
                return None
            bridge = self._bridge
            assert bridge is not None
            self._pending += 1
        result: Future[_T] = Future()

        def execute() -> None:
            try:
                result.set_result(callback())
            except BaseException as exc:  # noqa: BLE001 - release native waiters on every failure
                result.set_exception(exc)
                if native:
                    log.exception("OBS UI task failed")
            finally:
                self._finished()

        try:
            bridge.queue(execute, wait=wait)
        except BaseException:  # noqa: BLE001 - balance pending work when native enqueue fails
            self._finished()
            raise
        return result

    def _finished(self) -> None:
        with self._condition:
            self._pending -= 1
            self._condition.notify_all()
