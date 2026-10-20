"""Platform-neutral broker protocol and deterministic lifecycle regressions."""
from __future__ import annotations

import ctypes
import struct
import sys
import threading
from collections import deque
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from solin.core.scenes import windows_vcam_broker as broker_module
from solin.core.scenes.windows_vcam_broker import (
    VcamBrokerEndpoint,
    WindowsVcamBroker,
    build_response_bytes,
)
from solin.core.scenes.windows_vcam_transport import (
    BrokerStatus,
    encode_request,
    mapping_size_for,
    nv12_layout,
)


def _status(frame: bytes) -> int:
    return struct.unpack_from("<I", frame, 12)[0]


def _nonce(frame: bytes) -> bytes:
    return bytes(frame[16:32])


def _endpoint() -> VcamBrokerEndpoint:
    layout = nv12_layout(1280, 720)
    return VcamBrokerEndpoint(
        mapping_file_path_utf8=r"C:\Temp\Solin.VirtualCamera.{G}.frames",
        mapping_size=mapping_size_for(layout),
        generation=99,
        layout=layout,
        fps_numerator=30,
        fps_denominator=1,
    )


def test_junk_request_yields_invalid_request_with_zeroed_nonce():
    frame = build_response_bytes(b"not a request", _endpoint(), authorized=True)
    assert _status(frame) == int(BrokerStatus.INVALID_REQUEST)
    assert _nonce(frame) == bytes(16)


def test_unauthorized_client_is_denied_but_nonce_echoed():
    nonce = bytes(range(16))
    frame = build_response_bytes(encode_request(nonce), _endpoint(), authorized=False)
    assert _status(frame) == int(BrokerStatus.UNAUTHORIZED)
    assert _nonce(frame) == nonce


def test_missing_endpoint_reports_transport_unavailable():
    nonce = bytes(range(16))
    frame = build_response_bytes(encode_request(nonce), None, authorized=True)
    assert _status(frame) == int(BrokerStatus.TRANSPORT_UNAVAILABLE)
    assert _nonce(frame) == nonce


def test_ok_response_carries_the_ring_coordinates():
    nonce = bytes(range(16))
    endpoint = _endpoint()
    frame = build_response_bytes(encode_request(nonce), endpoint, authorized=True)
    assert _status(frame) == int(BrokerStatus.OK)
    assert _nonce(frame) == nonce
    mapping_size, generation = struct.unpack_from("<QQ", frame, 32)
    assert mapping_size == endpoint.mapping_size
    assert generation == 99
    fps_num, fps_den = struct.unpack_from("<II", frame, 88)
    assert (fps_num, fps_den) == (30, 1)
    path_len = struct.unpack_from("<H", frame, 96)[0]
    assert frame[128:128 + path_len].decode("utf-8") == endpoint.mapping_file_path_utf8


@pytest.mark.skipif(sys.platform == "win32", reason="guard is for non-Windows hosts")
def test_broker_construction_refused_off_windows():
    from solin.core.scenes.windows_vcam_broker import WindowsVcamBroker

    with pytest.raises(RuntimeError):
        WindowsVcamBroker("pipe", "S-1-5", lambda: None)


class _Thread:
    """Control worker lifetime without scheduling races or native handles."""

    def __init__(self, *, target, args, name, daemon):
        self.target, self.args = target, args
        self.alive = False
        self.started = False
        self.finish_on_join = True
        self.join_timeouts = []

    def start(self):
        self.started = self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        assert self.started, "must not join a thread that failed to start"
        self.join_timeouts.append(timeout)
        if self.alive and self.finish_on_join:
            self.target(*self.args)
            self.alive = False


class _Win:
    def __init__(self):
        self.event = threading.Event()
        self.created = []
        self.closed = []
        self.disconnected = []
        self.connect_results = deque()
        self.connect_calls = 0
        self.retry_waits = []
        self.fail_pipe_at = None

    def create_event(self):
        return self.event

    def create_pipe(self, name, sid, first_instance):
        if len(self.created) == self.fail_pipe_at:
            raise OSError("pipe creation failed")
        pipe = object()
        self.created.append((pipe, first_instance))
        return pipe

    def close_handle(self, handle):
        assert handle not in self.closed, "double handle close"
        self.closed.append(handle)

    def disconnect(self, pipe):
        self.disconnected.append(pipe)

    def set_event(self, event):
        assert event not in self.closed, "signaling a disposed event"
        event.set()

    def stop_requested(self, event, timeout_ms=0):
        assert event not in self.closed, "worker used a disposed event"
        if timeout_ms:
            self.retry_waits.append(timeout_ms)
        return event.is_set()

    def connect(self, pipe, stop_event):
        self.connect_calls += 1
        outcome = self.connect_results.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def lifecycle(monkeypatch):
    win = _Win()
    monkeypatch.setattr(broker_module, "require_windows", lambda: None)
    monkeypatch.setattr(broker_module, "_Win32", lambda: win)
    monkeypatch.setattr(broker_module.threading, "Thread", _Thread)
    broker = WindowsVcamBroker("isolated-test-pipe", "S-1-5-21-test", lambda: None)
    yield broker, win
    # Force deterministic completion even if the tested stop timed out.
    for thread in broker._threads:
        assert isinstance(thread, _Thread)
        thread.finish_on_join = True
    broker.stop()


@pytest.mark.parametrize("outcome", [False, OSError(232, "client aborted")])
def test_accept_failure_does_not_retire_worker(lifecycle, monkeypatch, outcome):
    broker, win = lifecycle
    broker.start()
    win.connect_results.extend([outcome, True])
    handled = Mock(side_effect=lambda *args: win.event.set())
    monkeypatch.setattr(broker, "_handle_client", handled)

    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False

    assert win.connect_calls == 2
    handled.assert_called_once()
    assert win.retry_waits and all(delay > 0 for delay in win.retry_waits)


def test_client_io_error_does_not_retire_worker(lifecycle, monkeypatch):
    broker, win = lifecycle
    broker.start()
    win.connect_results.extend([True, True])

    def handle(*args):
        if handled.call_count == 1:
            raise OSError(109, "client disconnected during handshake")
        win.event.set()

    handled = Mock(side_effect=handle)
    monkeypatch.setattr(broker, "_handle_client", handled)
    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False

    assert handled.call_count == 2
    assert win.retry_waits


def test_health_requires_started_and_every_worker_alive(lifecycle):
    broker, _ = lifecycle
    assert not broker.healthy
    broker.start()
    assert broker.healthy
    thread = broker._threads[1]
    thread.alive = False
    assert not broker.healthy
    thread.alive = True
    broker.stop()
    assert not broker.healthy


@pytest.mark.parametrize("failure_at", range(4))
def test_pipe_startup_failure_is_atomic(lifecycle, failure_at):
    broker, win = lifecycle
    win.fail_pipe_at = failure_at
    with pytest.raises(OSError, match="pipe creation failed"):
        broker.start()
    assert not broker._started
    assert not any(thread.is_alive() for thread in broker._threads)
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}


@pytest.mark.parametrize("failure_at", range(4))
def test_thread_startup_failure_cleans_started_and_unstarted_workers(
    lifecycle, monkeypatch, failure_at,
):
    broker, win = lifecycle
    starts = 0

    def start(thread):
        nonlocal starts
        if starts == failure_at:
            raise RuntimeError("thread start failed")
        starts += 1
        thread.started = thread.alive = True

    monkeypatch.setattr(_Thread, "start", start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        broker.start()
    assert not broker._started
    assert not any(thread.is_alive() for thread in broker._threads)
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}


def test_stop_retains_event_until_last_worker_finishes(lifecycle):
    broker, win = lifecycle
    broker.start()
    threads = list(broker._threads)
    for thread in threads:
        thread.finish_on_join = False
    broker.stop()
    assert win.event not in win.closed
    assert not broker.healthy
    with pytest.raises(RuntimeError, match="stopping"):
        broker.start()
    assert len(win.created) == 4

    # Completion after the shutdown deadline must release resources without
    # requiring another stop(), and must never use a closed stop event.
    for thread in threads:
        thread.target(*thread.args)
        thread.alive = False
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}
    broker.stop()


def test_shutdown_uses_one_deadline_for_all_workers(lifecycle, monkeypatch):
    broker, _ = lifecycle
    broker.start()
    threads = list(broker._threads)
    for thread in threads:
        thread.finish_on_join = False
    # Each join consumes a second; a per-thread timeout must not reset the budget.
    ticks = iter(range(100))
    monkeypatch.setattr(broker_module, "monotonic", lambda: next(ticks), raising=False)
    broker.stop()
    timeouts = [thread.join_timeouts[0] for thread in threads]
    assert timeouts == sorted(timeouts, reverse=True)
    assert len(set(timeouts)) > 1


def test_pipe_instances_are_reserved_before_workers_start_and_reused(lifecycle, monkeypatch):
    broker, win = lifecycle
    broker.start()
    assert len(win.created) == 4
    assert [first for _, first in win.created] == [True, False, False, False]
    win.connect_results.extend([True, True])

    def handle(*args):
        assert not any(pipe in win.closed for pipe, _ in win.created)
        if handled.call_count == 2:
            win.event.set()

    handled = Mock(side_effect=handle)
    monkeypatch.setattr(broker, "_handle_client", handled)
    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False
    assert handled.call_count == 2
    assert len(win.created) == 4


def test_event_creation_failure_does_not_allocate_pipes(lifecycle, monkeypatch):
    broker, win = lifecycle
    monkeypatch.setattr(win, "create_event", Mock(side_effect=OSError("no event")))
    with pytest.raises(OSError, match="no event"):
        broker.start()
    assert not broker.healthy
    assert not win.created
    assert not win.closed


def test_thread_construction_failure_releases_all_reservations(lifecycle, monkeypatch):
    broker, win = lifecycle
    monkeypatch.setattr(
        broker_module.threading, "Thread", Mock(side_effect=RuntimeError("no thread")),
    )
    with pytest.raises(RuntimeError, match="no thread"):
        broker.start()
    assert not broker.healthy
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}


def test_partial_startup_retains_resources_for_delayed_worker(lifecycle, monkeypatch):
    broker, win = lifecycle

    def start(thread):
        if any(other.is_alive() for other in broker._threads):
            raise RuntimeError("thread start failed")
        thread.started = thread.alive = True
        thread.finish_on_join = False

    monkeypatch.setattr(_Thread, "start", start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        broker.start()
    assert not broker.healthy
    assert win.event not in win.closed
    assert len(broker._threads) == 1
    assert set(win.closed) == {pipe for pipe, _ in win.created[1:]}
    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}


def test_interrupted_start_cannot_transfer_disposed_handles_to_worker(lifecycle, monkeypatch):
    broker, win = lifecycle
    launched = []

    def interrupted_start(thread):
        thread.started = thread.alive = True
        launched.append(thread)
        raise KeyboardInterrupt

    monkeypatch.setattr(_Thread, "start", interrupted_start)
    with pytest.raises(KeyboardInterrupt):
        broker.start()
    assert not broker.healthy
    assert set(win.closed) == {win.event, *(pipe for pipe, _ in win.created)}
    # The native thread may enter its target only after start() was interrupted
    # and rollback finished. It must decline ownership of the disposed handles.
    thread = launched[0]
    thread.target(*thread.args)
    thread.alive = False
    assert broker._event_users == 0


def test_fatal_server_error_retires_worker_instead_of_retrying(lifecycle):
    broker, win = lifecycle
    broker.start()
    win.connect_results.append(OSError(6, "invalid server handle"))
    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False
    assert win.connect_calls == 1
    assert not win.retry_waits
    assert not broker.healthy


def test_repeated_accept_errors_wait_and_stop_without_another_attempt(lifecycle, monkeypatch):
    broker, win = lifecycle
    broker.start()
    win.connect_results.extend([False, False, OSError(233, "client disconnected")])
    stop_requested = win.stop_requested

    def stop_after_retries(event, timeout_ms=0):
        if timeout_ms and len(win.retry_waits) == 2:
            event.set()
        return stop_requested(event, timeout_ms)

    monkeypatch.setattr(win, "stop_requested", stop_after_retries)
    thread = broker._threads[0]
    thread.target(*thread.args)
    thread.alive = False
    assert win.connect_calls == 3
    assert len(win.retry_waits) == 3
    assert all(timeout > 0 for timeout in win.retry_waits)


def test_client_identity_denial_never_enters_presence_drain(lifecycle, monkeypatch):
    broker, win = lifecycle
    pipe = object()
    nonce = bytes(range(16))
    identity = Mock(return_value=False)
    write = Mock(return_value=True)
    drain = Mock()
    monkeypatch.setattr(win, "read_exact", Mock(return_value=encode_request(nonce)), raising=False)
    monkeypatch.setattr(win, "client_matches_identity", identity, raising=False)
    monkeypatch.setattr(win, "write_exact", write, raising=False)
    monkeypatch.setattr(win, "drain_presence", drain, raising=False)
    monkeypatch.setattr(broker, "_endpoint_provider", _endpoint)
    broker._handle_client(pipe, win.event)
    identity.assert_called_once_with(pipe, broker._sid)
    response = write.call_args.args[1]
    assert _status(response) == int(BrokerStatus.UNAUTHORIZED)
    assert _nonce(response) == nonce
    assert response[32:] == bytes(len(response) - 32)
    drain.assert_not_called()


@pytest.fixture
def overlapped_win(monkeypatch):
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("hEvent", wintypes.HANDLE)]

    # Exercise actual syscall control flow without loading a Windows DLL.
    win = object.__new__(broker_module._Win32Impl)
    monkeypatch.setattr(win, "_ctypes", ctypes, raising=False)
    monkeypatch.setattr(win, "_wintypes", wintypes, raising=False)
    monkeypatch.setattr(win, "OVERLAPPED", Overlapped, raising=False)
    kernel = Mock()
    win._k32 = kernel
    kernel.CreateEventW.return_value = 101
    kernel.ConnectNamedPipe.return_value = False
    kernel.ReadFile.return_value = False
    kernel.WriteFile.return_value = False
    kernel.WaitForSingleObject.return_value = win.WAIT_TIMEOUT
    kernel.WaitForMultipleObjects.return_value = win.WAIT_TIMEOUT
    kernel.GetOverlappedResult.return_value = False
    monkeypatch.setattr(broker_module, "windows_last_error", lambda: win.ERROR_IO_PENDING)
    return win


def _invoke_io(win, operation):
    if operation == "connect":
        return win.connect(201, 301)
    if operation == "read_exact":
        return win.read_exact(201, 32, 301, 2000)
    if operation == "write_exact":
        return win.write_exact(201, b"response", 301, 2000)
    return win.drain_presence(201, 301)


@pytest.mark.parametrize("operation", ["connect", "read_exact", "write_exact", "drain_presence"])
@pytest.mark.parametrize("wait_result", [0, 258, 0xFFFFFFFF])
def test_pending_io_is_cancelled_and_drained_before_event_disposal(
    overlapped_win, operation, wait_result,
):
    win = overlapped_win
    kernel = win._k32
    kernel.WaitForMultipleObjects.return_value = wait_result
    expected = (
        pytest.raises(OSError, match="WaitForMultipleObjects")
        if wait_result == 0xFFFFFFFF else nullcontext()
    )
    with expected:
        assert not _invoke_io(win, operation)
    calls = [call[0] for call in kernel.mock_calls]
    assert calls.index("CancelIoEx") < calls.index("GetOverlappedResult") < calls.index("CloseHandle")
    assert kernel.GetOverlappedResult.call_args.args[-1] is True
    kernel.CloseHandle.assert_called_once_with(101)


@pytest.mark.parametrize("operation", ["connect", "read_exact", "write_exact", "drain_presence"])
@pytest.mark.parametrize("error", [6, 109, 232, 233, 995])
@pytest.mark.parametrize("pending", [False, True])
def test_io_distinguishes_client_disconnect_from_fatal_server_error(
    overlapped_win, monkeypatch, operation, error, pending,
):
    win = overlapped_win
    errors = Mock(side_effect=[win.ERROR_IO_PENDING, error] if pending else [error])
    monkeypatch.setattr(broker_module, "windows_last_error", errors)
    win._k32.WaitForMultipleObjects.return_value = win.WAIT_OBJECT_0 + 1
    with pytest.raises(OSError) if error == 6 else nullcontext():
        assert not _invoke_io(win, operation)
    win._k32.CloseHandle.assert_called_once_with(101)
