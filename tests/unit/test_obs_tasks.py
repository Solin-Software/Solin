from __future__ import annotations

import ctypes
import gc
from queue import Queue
from types import SimpleNamespace
import threading
import weakref

from cffi import FFI
import pylibobs._ffi as pylibobs_ffi
import pytest

from solin.core.media import obs_tasks


class _NativeLane:
    """Exercise the real CFFI bridge against the native queue's scheduling contract."""

    def __init__(self, ffi):
        self.ffi = ffi
        self.items = Queue()
        self.calls = []
        self.markers = []
        self.errors = []
        self.available = True
        self.mta_active = False
        self.worker = threading.Thread(target=self._work, name="test-native-destruction")
        self.worker.start()
        self.marker = ffi.callback(
            "void(void *)", lambda _param: self.markers.append(threading.get_ident())
        )

    def _work(self):
        while (item := self.items.get()) is not None:
            callback, done = item
            try:
                callback()
            except BaseException as exc:  # noqa: BLE001 - model native waiter release on every failure
                self.errors.append(exc)
            finally:
                if done is not None:
                    done.set()

    def schedule(self, callback, wait=False):
        if threading.current_thread() is self.worker:
            callback()
            return
        done = threading.Event() if wait else None
        self.items.put((callback, done))
        if done is not None:
            assert done.wait(4), "Native queue waiter stalled"

    def obs_queue_task(self, kind, task, param, wait):
        assert self.available, "OBS API used after native shutdown"
        assert self.mta_active, "Native work dispatched without an MTA guard"
        assert kind == 3
        self.calls.append((kind, wait))
        concrete = self.ffi.cast("void (*)(void *)", task)
        self.schedule(lambda: concrete(param), wait)

    def obs_in_task_thread(self, kind):
        assert self.available, "OBS API used after native shutdown"
        assert kind == 3
        return threading.current_thread() is self.worker

    def obs_set_ui_task_handler(self, pointer):
        assert self.mta_active, "UI handler registered before MTA acquisition"
        self.handler = self.ffi.cast("void (*)(void (*)(void *), void *, bool)", pointer)
        self.handler(self.marker, self.ffi.NULL, False)

    def ui(self, callback, wait=False):
        owner = self.ffi.callback("void(void *)", lambda _param: callback())
        self.handler(owner, self.ffi.NULL, wait)
        return owner  # The caller owns a native task function until its execution completes.

    def close(self):
        self.items.put(None)
        self.worker.join(4)
        assert not self.worker.is_alive()
        assert not self.errors


@pytest.fixture
def dispatcher(monkeypatch):
    ffi = FFI()
    ffi.cdef("""
        typedef _Bool bool;
        typedef void (*obs_task_t)(void *);
        typedef void (*obs_task_handler_t)(obs_task_t, void *, bool);
    """)
    lane = _NativeLane(ffi)
    com = []

    def retain_mta():
        lane.mta_active = True
        com.append(("acquire", threading.get_ident()))

        def release():
            assert lane.mta_active
            lane.mta_active = False
            com.append(("release", threading.get_ident()))

        return release

    monkeypatch.setattr(pylibobs_ffi, "ffi", ffi)
    monkeypatch.setattr(pylibobs_ffi, "get_lib", lambda: lane)
    monkeypatch.setattr(obs_tasks, "_retain_mta", retain_mta)
    tasks = obs_tasks.ObsTaskDispatcher()
    try:
        yield tasks, lane, com
    finally:
        tasks.quiesce()
        tasks.close()
        lane.close()


def test_install_retains_mta_before_registering_and_dispatching_native_work(dispatcher):
    tasks, lane, com = dispatcher
    tasks.install()
    tasks.install()
    assert com == [("acquire", threading.get_ident())]
    assert lane.markers == [lane.worker.ident]
    assert tasks.run(lambda: (37, threading.get_ident())) == (37, lane.worker.ident)


def test_ui_resets_share_ordering_with_native_source_destruction(dispatcher):
    tasks, lane, _com = dispatcher
    tasks.install()
    entered, release = threading.Event(), threading.Event()
    events = []

    def blocker():
        entered.set()
        assert release.wait(3)

    owners = [lane.ui(blocker)]
    assert entered.wait(3)
    owners.append(lane.ui(lambda: events.append(("reset-before", threading.get_ident()))))
    lane.schedule(lambda: events.append(("native-destroy", threading.get_ident())))
    owners.append(lane.ui(lambda: events.append(("reset-after", threading.get_ident()))))
    release.set()
    tasks.run(lambda: events.append(("monitor-create", threading.get_ident())))
    assert [event for event, _thread in events] == [
        "reset-before",
        "native-destroy",
        "reset-after",
        "monitor-create",
    ]
    assert {thread for _event, thread in events} == {lane.worker.ident}
    assert owners


def test_native_sync_submission_blocks_until_execution_finishes(dispatcher):
    tasks, lane, _com = dispatcher
    tasks.install()
    entered, release, returned = (threading.Event() for _ in range(3))

    def callback():
        entered.set()
        assert release.wait(3)

    def submit():
        owner = lane.ui(callback, wait=True)
        returned.set()
        assert owner

    caller = threading.Thread(target=submit)
    caller.start()
    try:
        assert entered.wait(3)
        assert not returned.is_set()
        release.set()
        assert returned.wait(3)
    finally:
        release.set()
        caller.join(4)
    assert not caller.is_alive()


@pytest.mark.parametrize("wait", [False, True])
def test_reentrant_native_submissions_and_run_execute_inline(dispatcher, wait):
    tasks, lane, _com = dispatcher
    tasks.install()
    events = []

    def outer():
        events.append("outer")
        owner = lane.ui(lambda: events.append("native-inner"), wait)
        result = tasks.run(lambda: events.append("python-inner") or 42)
        events.append(result)
        assert owner

    tasks.run(outer)
    assert events == ["outer", "native-inner", "python-inner", 42]


def test_quiesce_drains_and_retains_mta_and_inert_callbacks_until_close(dispatcher):
    tasks, lane, com = dispatcher
    tasks.install()
    bridge = weakref.ref(tasks._bridge)
    owner = lane.ui(lambda: com.append(("accepted", threading.get_ident())))
    tasks.quiesce()
    assert com == [("acquire", threading.get_ident()), ("accepted", lane.worker.ident)]
    assert lane.mta_active
    assert bridge() is not None
    lane.ui(lambda: pytest.fail("quiescent UI handler must be inert"), wait=True)
    with pytest.raises(RuntimeError, match="not accepting"):
        tasks.run(lambda: None)
    # Native source destruction can still happen after quiescence.
    lane.schedule(lambda: com.append(("native-destroy", lane.mta_active)), wait=True)
    assert com[-1] == ("native-destroy", True)
    lane.available = False  # Native shutdown returns; close must call no native API.
    tasks.quiesce()
    tasks.close()
    tasks.close()
    assert not lane.mta_active
    assert [event for event, _value in com] == ["acquire", "accepted", "native-destroy", "release"]
    gc.collect()
    assert bridge() is None
    assert owner


@pytest.mark.parametrize("error", [RuntimeError, SystemExit])
def test_run_propagates_task_errors_without_losing_native_waiters(dispatcher, error):
    tasks, _lane, _com = dispatcher
    tasks.install()

    def fail():
        raise error("operation failed")

    with pytest.raises(error, match="operation failed"):
        tasks.run(fail)
    assert tasks.run(lambda: 42) == 42


@pytest.mark.parametrize("wait", [False, True])
def test_native_task_errors_release_waiters_and_leave_lane_usable(
    dispatcher, monkeypatch, caplog, wait
):
    tasks, lane, _com = dispatcher
    tasks.install()
    # Bypass an additional Python CFFI callback so the task itself raises in the
    # dispatcher's execution boundary, as a failed Python monitoring operation can.
    bridge = tasks._bridge
    assert bridge is not None

    def fail():
        raise RuntimeError("native operation failed")

    tasks._dispatch(fail, wait)
    assert tasks.run(lambda: 42) == 42
    assert "OBS UI task failed" in caplog.text


def test_failed_native_enqueue_does_not_leave_pending_work_or_handles(dispatcher, monkeypatch):
    tasks, lane, _com = dispatcher
    tasks.install()
    queue_task = lane.obs_queue_task

    def fail(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(lane, "obs_queue_task", fail)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        tasks.run(lambda: None)
    assert tasks._bridge is not None and not tasks._bridge._handles
    monkeypatch.setattr(lane, "obs_queue_task", queue_task)
    tasks.quiesce()


def test_mta_acquisition_failure_dispatches_no_native_work_and_can_be_closed(
    dispatcher, monkeypatch
):
    tasks, lane, com = dispatcher

    def fail():
        raise OSError("MTA unavailable")

    monkeypatch.setattr(obs_tasks, "_retain_mta", fail)
    with pytest.raises(OSError, match="MTA unavailable"):
        tasks.install()
    assert com == []
    assert not lane.calls
    assert not hasattr(lane, "handler")
    with pytest.raises(RuntimeError, match="not accepting"):
        tasks.run(lambda: None)
    lane.available = False
    tasks.close()


def test_registration_failure_retains_mta_and_inert_handler_until_close(dispatcher, monkeypatch):
    tasks, lane, com = dispatcher
    register = lane.obs_set_ui_task_handler

    def fail(pointer):
        register(pointer)
        raise RuntimeError("registration failed")

    monkeypatch.setattr(lane, "obs_set_ui_task_handler", fail)
    with pytest.raises(RuntimeError, match="registration failed"):
        tasks.install()
    assert [event for event, _thread in com] == ["acquire"]
    assert lane.mta_active
    lane.ui(lambda: pytest.fail("callback must be inert"), wait=True)
    assert tasks._bridge is not None
    lane.available = False
    tasks.close()
    tasks.close()
    assert [event for event, _thread in com] == ["acquire", "release"]


def test_bridge_creation_failure_retains_guard_until_close(dispatcher, monkeypatch):
    tasks, lane, com = dispatcher

    def fail(_dispatch):
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(obs_tasks, "_NativeTaskBridge", fail)
    with pytest.raises(RuntimeError, match="bridge unavailable"):
        tasks.install()
    assert lane.mta_active
    assert not lane.calls
    lane.available = False
    tasks.close()
    assert [event for event, _thread in com] == ["acquire", "release"]


def test_close_retains_guard_if_release_fails_and_allows_retry(dispatcher, monkeypatch):
    tasks, lane, com = dispatcher
    tasks.install()
    tasks.quiesce()
    release = tasks._release_mta

    def fail():
        raise OSError("MTA release failed")

    monkeypatch.setattr(tasks, "_release_mta", fail)
    lane.available = False
    with pytest.raises(OSError, match="MTA release failed"):
        tasks.close()
    assert lane.mta_active
    monkeypatch.setattr(tasks, "_release_mta", release)
    tasks.close()
    tasks.close()
    assert [event for event, _thread in com] == ["acquire", "release"]


def test_lifecycle_rejects_calls_from_the_native_lane_and_requires_quiescence(dispatcher):
    tasks, _lane, _com = dispatcher
    tasks.install()
    with pytest.raises(RuntimeError, match="destruction lane"):
        tasks.run(tasks.quiesce)
    with pytest.raises(RuntimeError, match="Quiesce OBS tasks"):
        tasks.close()
    assert tasks.run(lambda: 42) == 42


def test_close_without_install_is_safe_and_reinstallation_is_rejected(dispatcher):
    tasks, _lane, _com = dispatcher
    tasks.close()
    tasks.close()
    with pytest.raises(RuntimeError, match="cannot be reinstalled"):
        tasks.install()
    with pytest.raises(RuntimeError, match="not accepting"):
        tasks.run(lambda: None)


@pytest.mark.parametrize(
    "acquire_result, release_result", [(0, 0), (-2147467259, 0), (0, -2147467259)]
)
def test_mta_guard_balances_cookie_and_checks_failed_hresult(
    monkeypatch, acquire_result, release_result
):
    monkeypatch.setattr(obs_tasks.sys, "platform", "win32")
    calls = []

    class Function:
        def __init__(self, call):
            self._call = call

        def __call__(self, *args):
            return self._call(*args)

    def acquire(pointer):
        calls.append("acquire")
        if acquire_result >= 0:
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 0x1234
        return acquire_result

    increment = Function(acquire)
    decrement = Function(lambda cookie: calls.append(("release", cookie.value)) or release_result)
    ole32 = SimpleNamespace(CoIncrementMTAUsage=increment, CoDecrementMTAUsage=decrement)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: ole32, raising=False)
    if acquire_result < 0:
        with pytest.raises(OSError, match="acquisition failed: 0x80004005"):
            obs_tasks._retain_mta()
    else:
        release = obs_tasks._retain_mta()
        gc.collect()  # The cleanup callable must retain the cookie and DLL owner.
        assert calls == ["acquire"]
        if release_result < 0:
            with pytest.raises(OSError, match="release failed: 0x80004005"):
                release()
        else:
            release()
    assert calls == ["acquire"] + ([] if acquire_result < 0 else [("release", 0x1234)])
    assert increment.argtypes == (ctypes.POINTER(ctypes.c_void_p),)
    assert increment.restype is ctypes.c_int32
    assert decrement.argtypes == (ctypes.c_void_p,)
    assert decrement.restype is ctypes.c_int32


def test_async_task_bridge_retains_handle_until_trampoline_finishes(monkeypatch):
    import gc

    from cffi import FFI

    ffi = FFI()
    ffi.cdef("""
        typedef void (*obs_task_t)(void *);
        typedef void (*obs_task_handler_t)(obs_task_t, void *, _Bool);
    """)
    queued = []

    def queue(kind, task, handle, wait):
        # Native OBS retains only an address, never the Python handle owner.
        borrowed = ffi.cast("void *", int(ffi.cast("uintptr_t", handle)))
        queued.append((kind, task, borrowed, wait))

    monkeypatch.setattr(pylibobs_ffi, "ffi", ffi)
    monkeypatch.setattr(
        pylibobs_ffi,
        "get_lib",
        lambda: SimpleNamespace(obs_queue_task=queue),
    )
    bridge = obs_tasks._NativeTaskBridge(lambda _task, _wait: None)
    executed = []

    class Payload:
        def __call__(self):
            executed.append("executed")

    payload = Payload()
    reference = weakref.ref(payload)
    bridge.queue(payload, wait=False)
    del payload
    gc.collect()
    assert reference() is not None
    assert len(bridge._handles) == 1
    kind, pointer, param, wait = queued.pop()
    assert kind == 3 and wait is False
    ffi.cast("void (*)(void *)", pointer)(param)
    gc.collect()
    assert executed == ["executed"]
    assert not bridge._handles
    assert reference() is None
