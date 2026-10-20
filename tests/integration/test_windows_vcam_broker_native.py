"""Real Win32 broker lifecycle tests using only UUID-scoped test endpoints."""

from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager

import pytest

from solin.core.scenes.windows_vcam_broker import (
    VcamBrokerEndpoint,
    WindowsVcamBroker,
    _Win32Impl,
)
from solin.core.scenes.windows_vcam_identity import current_user_sid
from solin.core.scenes.windows_vcam_transport import (
    RESPONSE_SIZE,
    BrokerStatus,
    encode_request,
    mapping_size_for,
    nv12_layout,
)


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Win32 named pipes")


@pytest.fixture
def pipe_name():
    # Never derive this from current_user_broker_pipe_name(): the installed app
    # owns that endpoint and may be serving real camera clients during tests.
    return rf"\\.\pipe\Solin.Tests.VirtualCamera.{uuid.uuid4().hex}"


@pytest.fixture
def broker_factory(request, pipe_name, tmp_path):
    layout = nv12_layout(64, 32)
    endpoint = VcamBrokerEndpoint(
        str(tmp_path / "test.frames"), mapping_size_for(layout), 1, layout, 30, 1,
    )

    def create(provider=lambda: endpoint):
        broker = WindowsVcamBroker(pipe_name, current_user_sid(), provider)

        def cleanup():
            threads = list(broker._threads)
            broker.stop()
            assert not any(thread.is_alive() for thread in threads)

        request.addfinalizer(cleanup)
        return broker

    return create


@pytest.fixture
def client_api():
    from ctypes import wintypes

    win = _Win32Impl()
    win._k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    win._k32.CreateFileW.restype = wintypes.HANDLE
    return win


@contextmanager
def _client(win, pipe_name):
    deadline = time.monotonic() + 3.0
    while True:
        handle = win._k32.CreateFileW(
            pipe_name, 0xC0000000, 0, None, 3, win.FILE_FLAG_OVERLAPPED, None,
        )
        if handle != win.INVALID_HANDLE_VALUE:
            break
        error = ctypes.get_last_error()
        if error not in (2, 231) or time.monotonic() >= deadline:
            raise OSError(error, "could not connect to isolated test pipe")
        time.sleep(0.01)
    try:
        event = win.create_event()
        try:
            yield handle, event
        finally:
            win.close_handle(event)
    finally:
        win.close_handle(handle)


def _handshake(win, client, status=BrokerStatus.OK):
    handle, event = client
    nonce = uuid.uuid4().bytes
    assert win.write_exact(handle, encode_request(nonce), event, 1000)
    response = win.read_exact(handle, RESPONSE_SIZE, event, 1000)
    assert response is not None
    assert struct.unpack_from("<I", response, 12)[0] == int(status)
    assert response[16:32] == nonce


def test_aborts_before_accept_do_not_exhaust_workers(
    broker_factory, client_api, pipe_name, monkeypatch, request,
):
    broker = broker_factory()
    ready = threading.Barrier(5, timeout=3)
    release = threading.Event()
    # Release blocked workers before the broker fixture's cleanup even if a
    # client-open assertion fails.
    request.addfinalizer(release.set)
    connect = broker._win.connect
    seen = set()

    def delayed_connect(pipe, stop_event):
        if pipe not in seen:
            seen.add(pipe)
            ready.wait()
            assert release.wait(3)
        return connect(pipe, stop_event)

    monkeypatch.setattr(broker._win, "connect", delayed_connect)
    broker.start()
    ready.wait()
    with ExitStack() as clients:
        for _ in range(4):
            clients.enter_context(_client(client_api, pipe_name))
    # Every pipe had a client disconnect before ConnectNamedPipe could run.
    release.set()
    with ExitStack() as clients:
        for _ in range(4):
            client = clients.enter_context(_client(client_api, pipe_name))
            _handshake(client_api, client)
        assert broker.healthy


def test_repeated_client_aborts_preserve_all_instances(broker_factory, client_api, pipe_name):
    broker = broker_factory()
    broker.start()
    for index in range(32):
        with _client(client_api, pipe_name) as (handle, event):
            if index % 2:
                assert client_api.write_exact(handle, b"SLN", event, 1000)
    with ExitStack() as clients:
        for _ in range(4):
            _handshake(client_api, clients.enter_context(_client(client_api, pipe_name)))
        assert broker.healthy


@pytest.mark.parametrize("phase", ["accept", "partial_request", "presence"])
def test_stop_cancels_pending_io_and_joins_workers(
    broker_factory, client_api, pipe_name, phase,
):
    broker = broker_factory()
    broker.start()
    threads = list(broker._threads)
    with ExitStack() as clients:
        if phase != "accept":
            for _ in range(4):
                client = clients.enter_context(_client(client_api, pipe_name))
                if phase == "presence":
                    _handshake(client_api, client)
                else:
                    handle, event = client
                    assert client_api.write_exact(handle, b"SLN", event, 1000)
        started = time.monotonic()
        broker.stop()
        assert time.monotonic() - started < 2.0
        assert not broker.healthy
        assert not any(thread.is_alive() for thread in threads)
        assert broker._stop_event is None


def test_first_instance_protection_survives_reuse(broker_factory, client_api, pipe_name):
    broker = broker_factory()
    broker.start()
    for _ in range(8):
        with _client(client_api, pipe_name) as client:
            _handshake(client_api, client)
    competitor = broker_factory()
    with pytest.raises(OSError):
        competitor.start()
    assert not competitor.healthy
    assert broker.healthy
    with _client(client_api, pipe_name) as client:
        _handshake(client_api, client)


def test_partial_native_startup_releases_endpoint(
    broker_factory, client_api, pipe_name, monkeypatch,
):
    broker = broker_factory()
    create_pipe = broker._win.create_pipe

    def fail_sibling(name, sid, first):
        if not first:
            raise OSError("injected pipe allocation failure")
        return create_pipe(name, sid, first)

    monkeypatch.setattr(broker._win, "create_pipe", fail_sibling)
    with pytest.raises(OSError, match="injected"):
        broker.start()
    assert not broker.healthy
    assert broker._stop_event is None
    replacement = broker_factory()
    replacement.start()
    with _client(client_api, pipe_name) as client:
        _handshake(client_api, client)


@pytest.mark.parametrize("error", [ValueError("unexpected failure"), OSError(6, "invalid handle")])
def test_unexpected_worker_exit_is_unhealthy(broker_factory, monkeypatch, error):
    broker = broker_factory()

    def crash(*args):
        raise error

    monkeypatch.setattr(broker._win, "connect", crash)
    broker.start()
    threads = list(broker._threads)
    for thread in threads:
        thread.join(timeout=1)
    assert not any(thread.is_alive() for thread in threads)
    assert not broker.healthy
    assert broker._stop_event is None


def test_interrupted_native_thread_start_releases_endpoint(
    broker_factory, client_api, pipe_name, monkeypatch,
):
    broker = broker_factory()
    launched = []
    start = threading.Thread.start

    def interrupted_start(thread):
        start(thread)
        launched.append(thread)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", interrupted_start)
        with pytest.raises(KeyboardInterrupt):
            broker.start()
    for thread in launched:
        thread.join(timeout=1)
    assert not any(thread.is_alive() for thread in launched)
    assert not broker.healthy
    assert broker._event_users == 0
    assert broker._stop_event is None
    replacement = broker_factory()
    replacement.start()
    with _client(client_api, pipe_name) as client:
        _handshake(client_api, client)


def test_stopped_broker_can_restart(broker_factory, client_api, pipe_name):
    broker = broker_factory()
    for _ in range(3):
        broker.start()
        assert broker.healthy
        with _client(client_api, pipe_name) as client:
            _handshake(client_api, client)
        broker.stop()
        assert not broker.healthy


def test_provider_failure_does_not_retire_worker(broker_factory, client_api, pipe_name):
    def provider():
        raise RuntimeError("endpoint temporarily unavailable")

    broker = broker_factory(provider)
    broker.start()
    for _ in range(8):
        with _client(client_api, pipe_name) as client:
            _handshake(client_api, client, BrokerStatus.TRANSPORT_UNAVAILABLE)
        assert broker.healthy
