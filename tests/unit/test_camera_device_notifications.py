"""Native notification contracts exercised without cameras or platform libraries."""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import os
import socket
import sys
import threading
import time
import uuid
import weakref
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from solin.core.media import camera_device_notifications as notifications


@pytest.fixture(autouse=True)
def release_subscriptions():
    before = set(notifications._live)
    yield
    owners = [owner for key, owner in notifications._live.copy().items() if key not in before]
    for owner in owners:
        owner.close()
    for owner in owners:
        if owner._cleanup_thread is not None:
            owner._cleanup_thread.join(2)
            assert not owner._cleanup_thread.is_alive()
    assert set(notifications._live) == before


def finish(subscription):
    subscription.close()
    subscription._cleanup_thread.join(2)
    assert not subscription._cleanup_thread.is_alive()
    assert id(subscription) not in notifications._live


@pytest.mark.parametrize("platform, adapter", [
    ("win32", "_WindowsSubscription"), ("darwin", "_MacSubscription"),
    ("linux", "_LinuxSubscription"),
])
def test_dispatch_only_constructs_selected_adapter(monkeypatch, platform, adapter):
    callback, expected = lambda: None, SimpleNamespace(close=lambda: None)
    calls = []
    for name in ("_WindowsSubscription", "_MacSubscription", "_LinuxSubscription"):
        def start(on_change, name=name):
            calls.append((name, on_change))
            return expected
        monkeypatch.setattr(notifications, name, start)
    monkeypatch.setattr(notifications.sys, "platform", platform)
    assert notifications.start_camera_device_notifications(callback) is expected
    assert calls == [(adapter, callback)]


def test_unsupported_platform_and_invalid_callback(monkeypatch):
    monkeypatch.setattr(notifications.sys, "platform", "unsupported")
    with pytest.raises(OSError, match="unsupported"):
        notifications.start_camera_device_notifications(lambda: None)
    with pytest.raises(TypeError, match="callable"):
        notifications.start_camera_device_notifications(None)


def test_module_import_does_not_load_native_libraries(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("native loading during module import")
    monkeypatch.setattr(ctypes, "CDLL", unexpected)
    monkeypatch.setattr(ctypes, "WinDLL", unexpected, raising=False)
    monkeypatch.setattr(notifications.importlib, "import_module", unexpected)
    spec = importlib.util.spec_from_file_location("isolated_camera_events", notifications.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh._live == {}


@pytest.mark.parametrize("error", [ImportError("missing"), ValueError("bridge failure")])
def test_loader_errors_have_typed_public_boundary(monkeypatch, error):
    def fail(_callback):
        raise error
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications, "_MacSubscription", fail)
    with pytest.raises(RuntimeError) as caught:
        notifications.start_camera_device_notifications(lambda: None)
    assert caught.value.__cause__ is error


class Function:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class WindowsLibrary:
    def __init__(self, *, fail_at=None):
        self.filters, self.callbacks, self.removed = [], {}, []
        self.fail_at = fail_at
        self.on_register = None
        self.unregister_result = 0
        self.release_allowed = threading.Event()
        self.release_allowed.set()
        self.unregister_entered = threading.Event()
        self.CM_Register_Notification = Function(self.register)
        self.CM_Unregister_Notification = Function(self.unregister)

    def register(self, native_filter, context, callback, output):
        assert context is None
        self.filters.append(native_filter._obj)
        if len(self.filters) == self.fail_at:
            return 0x13
        handle = len(self.filters)
        output._obj.value = handle
        self.callbacks[handle] = weakref.ref(callback)
        if self.on_register is not None:
            self.on_register(callback)
        return 0

    def unregister(self, handle):
        self.unregister_entered.set()
        assert self.release_allowed.wait(2)
        self.removed.append(handle.value)
        if not self.unregister_result:
            self.callbacks.pop(handle.value)
        return self.unregister_result

    def emit(self, action):
        for callback in list(self.callbacks.values()):
            assert callback() is not None
            assert callback()(None, None, action, None, 0) == 0


@pytest.fixture
def windows(monkeypatch):
    library = WindowsLibrary()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: library, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    yield library
    library.release_allowed.set()
    library.unregister_result = 0
    # A failed unregister intentionally pins ownership. Recover the fake before
    # the general finalizer so this failure scenario cannot leak into other tests.
    for owner in list(notifications._live.values()):
        if getattr(owner, "_lib", None) is library and owner._cleanup_thread is not None:
            owner._cleanup_thread.join(2)
            if id(owner) in notifications._live:
                owner._cleanup()


def test_windows_abi_filters_and_actions(windows):
    calls = []
    subscription = notifications._WindowsSubscription(lambda: calls.append("changed"))
    assert ctypes.sizeof(notifications._windows_abi()) == 416
    classes = []
    for event_filter in windows.filters:
        assert (event_filter.size, event_filter.flags, event_filter.kind,
                event_filter.reserved) == (416, 0, 0, 0)
        classes.append(str(uuid.UUID(bytes_le=bytes(event_filter.data.class_guid))))
    assert classes == ["e5323777-f976-4f5b-9b55-b94699c46e44",
                       "6994ad05-93ef-11d0-a3cc-00a0c9223196"]
    for action in (0, 1, 2, 7, 99):
        windows.emit(action)
    assert len(calls) == 4  # Each selected class delivers arrival/removal.
    callbacks = [callback() for callback in windows.callbacks.values()]
    finish(subscription)
    subscription.close()
    for callback in callbacks:
        callback(None, None, 0, None, 0)
    assert len(calls) == 4
    assert windows.removed == [1, 2]


@pytest.mark.parametrize("failure", [1, 2])
def test_windows_partial_registration_releases_successful_handles(windows, failure):
    windows.fail_at = failure
    with pytest.raises(OSError, match="CONFIGRET"):
        notifications._WindowsSubscription(lambda: None)
    assert windows.removed == list(range(1, failure))


def test_windows_callback_exceptions_do_not_cross_native_boundary(windows, caplog):
    def fail():
        raise ValueError("consumer failed")
    subscription = notifications._WindowsSubscription(fail)
    windows.emit(0)
    assert "consumer failed" in caplog.text
    finish(subscription)


def test_windows_close_is_bounded_and_pins_trampoline_until_unregister_finishes(windows):
    called = threading.Event()
    subscription = notifications._WindowsSubscription(called.set)
    reference = weakref.ref(subscription)
    windows.release_allowed.clear()
    started = time.monotonic()
    subscription.close()
    assert time.monotonic() - started < 0.8
    assert windows.unregister_entered.is_set()
    worker = subscription._cleanup_thread
    assert worker.is_alive()
    del subscription
    gc.collect()
    assert reference() is not None
    windows.emit(0)
    assert not called.is_set()
    windows.release_allowed.set()
    worker.join(2)
    assert not worker.is_alive()
    gc.collect()
    assert reference() is None


def test_windows_unregister_failure_retains_callback_without_delivering(windows, caplog):
    subscription = notifications._WindowsSubscription(lambda: pytest.fail("closed callback"))
    windows.unregister_result = 0x13
    subscription.close()
    subscription._cleanup_thread.join(2)
    assert id(subscription) in notifications._live
    assert subscription._native_callback is not None
    windows.emit(0)
    assert len(windows.removed) == 2
    assert "remain registered" in caplog.text


def test_close_can_retry_if_cleanup_thread_cannot_start(windows, monkeypatch, caplog):
    subscription = notifications._WindowsSubscription(lambda: pytest.fail("closed callback"))
    def fail(_worker):
        raise RuntimeError("thread unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        subscription.close()
    assert subscription._cleanup_thread is None
    assert id(subscription) in notifications._live
    windows.emit(0)
    assert "thread unavailable" in caplog.text
    finish(subscription)
    assert windows.removed == [1, 2]


def test_cleanup_scheduling_failure_preserves_original_start_error(windows, monkeypatch):
    windows.fail_at = 2
    def fail(_worker):
        raise RuntimeError("thread unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        with pytest.raises(OSError, match="CM_Register_Notification"):
            notifications._WindowsSubscription(lambda: None)
    owner = next(owner for owner in notifications._live.values()
                 if getattr(owner, "_lib", None) is windows)
    finish(owner)
    assert windows.removed == [1]


def test_close_during_callback_does_not_wait_for_consumer(windows):
    entered, resume = threading.Event(), threading.Event()
    calls = []
    def change():
        calls.append("entered")
        entered.set()
        assert resume.wait(2)
    subscription = notifications._WindowsSubscription(change)
    callback = next(iter(windows.callbacks.values()))()
    emitter = threading.Thread(target=lambda: callback(None, None, 0, None, 0))
    emitter.start()
    try:
        assert entered.wait(2)
        subscription.close()
        assert emitter.is_alive()
        callback(None, None, 0, None, 0)
        assert calls == ["entered"]
    finally:
        resume.set()
        emitter.join(2)
        finish(subscription)


def test_callback_may_close_subscription_reentrantly(windows):
    subscription = notifications._WindowsSubscription(lambda: subscription.close())
    callback = next(iter(windows.callbacks.values()))()
    emitter = threading.Thread(target=lambda: callback(None, None, 0, None, 0))
    emitter.start()
    emitter.join(2)
    assert not emitter.is_alive()
    finish(subscription)


def test_windows_close_before_registration_returns_waits_for_all_handles(windows):
    windows.on_register = lambda callback: callback(None, None, 0, None, 0)
    closed = []
    def change():
        owner = next(owner for owner in notifications._live.values()
                     if getattr(owner, "_lib", None) is windows)
        owner.close()
        closed.append(owner._native_callback is not None)
    subscription = notifications._WindowsSubscription(change)
    finish(subscription)
    assert closed == [True]
    assert windows.removed == [1, 2]


class NotificationCenter:
    def __init__(self):
        self.tokens, self.removed = [], []
        self.fail_at = None
        self.on_register = None
        self.discovery_started = False
        self.discovery_failure = False

    def addObserverForName_object_queue_usingBlock_(self, name, obj, queue, callback):
        assert obj is None and queue is None
        if len(self.tokens) + 1 == self.fail_at:
            raise ValueError("observer registration failed")
        token = SimpleNamespace(name=name, callback=callback)
        self.tokens.append(token)
        if self.on_register is not None:
            self.on_register(callback)
        return token

    def removeObserver_(self, token):
        self.removed.append(token)


@pytest.fixture
def macos(monkeypatch):
    center = NotificationCenter()
    loads = []
    def load_bundle(name, symbols, *, bundle_path, scan_classes):
        assert scan_classes is False
        loads.append((name, bundle_path))
        return "native bundle"
    def load_variables(bundle, symbols, variables, *, skip_undefined):
        assert bundle == "native bundle"
        assert skip_undefined is False
        symbols.update({name: name for name, encoding in variables if encoding == b"@"})
    def look_up_class(name):
        assert name == "AVCaptureDeviceDiscoverySession"
        def discover(device_types, media, position):
            assert len(center.tokens) == 2  # Observe before activating the native monitor.
            assert device_types == ["AVCaptureDeviceTypeBuiltInWideAngleCamera",
                                    "AVCaptureDeviceTypeExternalUnknown"]
            assert media is None and position == 0
            center.discovery_started = True
            if center.discovery_failure:
                return None
            return object()  # No inventory/capture API is available on this fake.
        return SimpleNamespace(discoverySessionWithDeviceTypes_mediaType_position_=discover)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(
        autorelease_pool=nullcontext, loadBundle=load_bundle, loadBundleVariables=load_variables,
        lookUpClass=look_up_class,
    ))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSNotificationCenter=SimpleNamespace(defaultCenter=lambda: center),
    ))
    return center, loads


def test_macos_loads_framework_and_filters_audio_only_devices(macos):
    center, loads = macos
    changes = []
    subscription = notifications._MacSubscription(lambda: changes.append("changed"))
    assert center.discovery_started
    assert subscription._discovery_session is not None
    assert loads == [("AVFoundation", "/System/Library/Frameworks/AVFoundation.framework")]
    assert [token.name for token in center.tokens] == [
        "AVCaptureDeviceWasConnectedNotification", "AVCaptureDeviceWasDisconnectedNotification",
    ]
    for token in center.tokens:
        for media in ("AVMediaTypeVideo", "AVMediaTypeMuxed", "AVMediaTypeAudio"):
            token.callback(SimpleNamespace(object=lambda media=media: SimpleNamespace(
                hasMediaType_=lambda candidate: candidate == media,
            )))
        token.callback(SimpleNamespace(object=lambda: None))
    assert len(changes) == 4
    finish(subscription)
    subscription.close()
    assert center.removed == center.tokens
    assert subscription._discovery_session is None
    center.tokens[0].callback(SimpleNamespace(object=lambda: SimpleNamespace(
        hasMediaType_=lambda media: True,
    )))
    assert len(changes) == 4


@pytest.mark.parametrize("failure", [1, 2])
def test_macos_partial_registration_is_released_and_normalized(macos, monkeypatch, failure):
    center, _ = macos
    center.fail_at = failure
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    with pytest.raises(RuntimeError) as caught:
        notifications.start_camera_device_notifications(lambda: None)
    assert isinstance(caught.value.__cause__, ValueError)
    assert center.removed == center.tokens


def test_macos_close_before_observer_registration_returns(macos):
    center, _ = macos
    center.on_register = lambda callback: callback(SimpleNamespace(
        object=lambda: SimpleNamespace(hasMediaType_=lambda media: True),
    ))
    def change():
        owner = next(owner for owner in notifications._live.values()
                     if getattr(owner, "_center", None) is center)
        owner.close()
    subscription = notifications._MacSubscription(change)
    finish(subscription)
    assert center.removed == center.tokens


def test_macos_discovery_activation_failure_removes_both_observers(macos):
    center, _ = macos
    center.discovery_failure = True
    with pytest.raises(RuntimeError, match="activate AVFoundation"):
        notifications._MacSubscription(lambda: None)
    assert center.discovery_started
    assert center.removed == center.tokens


class UdevLibrary:
    def __init__(self):
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.queue = deque()
        self.unreferenced = []
        self.processed = threading.Event()
        self.released = []
        self.failure = None
        self.receive_error = None
        self.receive_count = 0
        self.filters = []

    def udev_new(self):
        return None if self.failure == "context" else "context"

    def udev_monitor_new_from_netlink(self, context, group):
        assert context == "context" and group == b"udev"
        return None if self.failure == "monitor" else "monitor"

    def udev_monitor_filter_add_match_subsystem_devtype(self, monitor, subsystem, devtype):
        self.filters.append((monitor, subsystem, devtype))
        return -13 if self.failure == "filter" else 0

    def udev_monitor_enable_receiving(self, monitor):
        assert self.filters == [(monitor, b"video4linux", None)]
        return -13 if self.failure == "enable" else 0

    def udev_monitor_get_fd(self, monitor):
        return -1 if self.failure == "descriptor" else self.reader.fileno()

    def udev_monitor_receive_device(self, monitor):
        self.receive_count += 1
        if self.receive_error is not None:
            ctypes.set_errno(self.receive_error)
            return None
        try:
            self.reader.recv(1)
        except BlockingIOError:
            return None
        return self.queue.popleft()

    def udev_device_get_subsystem(self, device):
        return device[0]

    def udev_device_get_action(self, device):
        return device[1]

    def udev_device_unref(self, device):
        self.unreferenced.append(device)
        if not self.queue:
            self.processed.set()

    def udev_monitor_unref(self, monitor):
        self.released.append(monitor)

    def udev_unref(self, context):
        self.released.append(context)

    def emit(self, events):
        self.queue.extend(events)
        self.writer.send(bytes(len(events)))


@pytest.fixture
def udev(monkeypatch):
    library = UdevLibrary()
    monkeypatch.setattr(notifications, "_load_udev", lambda: library)
    # Linux fd setup is tested as a call; on Windows os.set_blocking cannot
    # operate on the real socket handle used for this hardware-free fake.
    monkeypatch.setattr(os, "set_blocking", lambda fd, blocking: None)
    yield library
    for owner in list(notifications._live.values()):
        if getattr(owner, "_lib", None) is library:
            finish(owner)
    library.reader.close()
    library.writer.close()


def test_linux_filters_events_and_releases_every_received_device(udev):
    changes = []
    subscription = notifications._LinuxSubscription(lambda: changes.append("changed"))
    events = [(b"video4linux", action) for action in (b"add", b"remove", b"change")]
    events += [(b"usb", b"add"), (b"video4linux", None), (b"video4linux", b"online")]
    udev.emit(events)
    assert udev.processed.wait(2)
    assert len(changes) == 3
    assert udev.unreferenced == events
    finish(subscription)
    assert udev.released == ["monitor", "context"]
    assert not subscription._worker.is_alive()
    assert subscription._reader.fileno() == subscription._writer.fileno() == -1


@pytest.mark.parametrize("failure, released", [
    ("context", []), ("monitor", ["context"]),
    ("filter", ["monitor", "context"]), ("enable", ["monitor", "context"]),
    ("descriptor", ["monitor", "context"]),
])
def test_linux_start_failure_releases_only_acquired_resources(udev, failure, released):
    udev.failure = failure
    with pytest.raises(OSError):
        notifications._LinuxSubscription(lambda: None)
    assert udev.released == released


def test_linux_receive_failure_stops_once_without_retry(udev, caplog):
    subscription = notifications._LinuxSubscription(lambda: pytest.fail("receive failed"))
    udev.receive_error = 5
    udev.emit([(b"video4linux", b"add")])
    subscription._worker.join(2)
    assert not subscription._worker.is_alive()
    finish(subscription)
    assert udev.receive_count == 1
    assert "udev monitor stopped" in caplog.text
    assert udev.released == ["monitor", "context"]


def test_linux_close_wakes_idle_monitor_without_polling(udev, monkeypatch):
    import selectors

    entered = threading.Event()
    real_selector = selectors.DefaultSelector
    class Selector(real_selector):
        def select(self, timeout=None):
            assert timeout is None
            entered.set()
            return super().select(timeout)
    monkeypatch.setattr(selectors, "DefaultSelector", Selector)
    subscription = notifications._LinuxSubscription(lambda: None)
    assert entered.wait(2)
    assert udev.receive_count == 0
    finish(subscription)
    assert not subscription._worker.is_alive()
    assert udev.receive_count == 0


@pytest.mark.parametrize("failure", [1, 2])
def test_linux_selector_registration_failure_is_synchronous_and_released(udev, monkeypatch, failure):
    import selectors

    real_selector = selectors.DefaultSelector
    selectors_created = []
    class Selector(real_selector):
        def __init__(self):
            super().__init__()
            self.count = 0
            self.closed = False
            selectors_created.append(self)

        def register(self, *args, **kwargs):
            self.count += 1
            if self.count == failure:
                raise OSError("selector registration failed")
            return super().register(*args, **kwargs)

        def close(self):
            self.closed = True
            super().close()

    monkeypatch.setattr(selectors, "DefaultSelector", Selector)
    with pytest.raises(OSError, match="selector registration failed"):
        notifications._LinuxSubscription(lambda: None)
    assert selectors_created[0].closed
    assert udev.released == ["monitor", "context"]


def test_linux_supports_high_monitor_descriptor_through_default_selector(udev, monkeypatch):
    import selectors

    real_selector = selectors.DefaultSelector
    registered = []
    class Selector(real_selector):
        def register(self, fileobj, events, data=None):
            registered.append(fileobj)
            return super().register(udev.reader if fileobj == 4096 else fileobj, events, data)
    monkeypatch.setattr(selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(udev, "udev_monitor_get_fd", lambda monitor: 4096)
    changes = []
    subscription = notifications._LinuxSubscription(lambda: changes.append(True))
    assert registered[0] == 4096
    udev.emit([(b"video4linux", b"add")])
    assert udev.processed.wait(2)
    finish(subscription)
    assert changes == [True]
