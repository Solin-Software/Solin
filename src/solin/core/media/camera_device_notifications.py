"""Native camera topology events, without enumeration, capture, or a UI dependency.

Callbacks run on native/monitor threads and must return promptly. ``close()``
disables delivery and waits at most 200 ms for native cleanup. A callback already
admitted may finish after close; consumers must marshal and guard queued work.
Native owners stay pinned until cleanup succeeds, including after a timeout.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)
_CLOSE_TIMEOUT = 0.2
_live: dict[int, _Subscription] = {}
_live_lock = threading.Lock()


class CameraDeviceSubscription(Protocol):
    """Own native notifications until the idempotent ``close()`` call."""

    def close(self) -> None: ...


def start_camera_device_notifications(on_change: Callable[[], None]) -> CameraDeviceSubscription:
    """Subscribe lazily; unavailable native support raises OSError/RuntimeError.

    Subscribe before the first inventory request to avoid missing arrivals during
    enumeration. Notification bursts and thread marshalling belong to the caller.
    """
    if not callable(on_change):
        raise TypeError("on_change must be callable")
    try:
        if sys.platform == "win32":
            return _WindowsSubscription(on_change)
        if sys.platform == "darwin":
            return _MacSubscription(on_change)
        if sys.platform == "linux":
            return _LinuxSubscription(on_change)
    except (OSError, RuntimeError):
        raise
    except Exception as error:  # noqa: BLE001 - normalize native loader/bridge failures
        raise RuntimeError("Native camera notifications are unavailable") from error
    raise OSError(errno.ENOSYS, f"Camera notifications are unsupported on {sys.platform}")


class _Subscription:
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._on_change: Callable[[], None] | None = on_change
        self._initialized = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        # A C function pointer is not a Python GC root. Keep ownership even if a
        # caller drops its subscription without closing it.
        with _live_lock:
            _live[id(self)] = self

    def _notify(self) -> None:
        with self._lock:
            callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - never unwind through a native callback
                log.exception("Camera device change callback failed")

    def close(self) -> None:
        with self._lock:
            self._on_change = None
            if self._cleanup_thread is not None:
                return
            worker = threading.Thread(
                target=self._cleanup, name="camera-notifications-close", daemon=True,
            )
            try:
                worker.start()
            except RuntimeError:
                # Keep native ownership pinned and let a later close retry.
                log.exception("Could not schedule camera notification cleanup")
                return
            self._cleanup_thread = worker
        # Unregister may drain a callback that itself called close(). Never wait
        # indefinitely or unregister on that callback's native thread.
        worker.join(_CLOSE_TIMEOUT)

    def _cleanup(self) -> None:
        try:
            # A native callback can close us before register/observer-add returns
            # its handle. Wait for every acquired resource to be published.
            self._initialized.wait()
            self._release()
        except Exception:  # noqa: BLE001 - retain native callbacks if unregister fails
            log.exception("Could not release camera device notifications")
        else:
            with _live_lock:
                _live.pop(id(self), None)

    def _release(self) -> None:
        raise NotImplementedError


def _windows_abi():
    """Fixed-width Windows ABI, also constructible by platform-neutral tests."""
    u32, pointer = ctypes.c_uint32, ctypes.c_void_p

    class Guid(ctypes.Structure):
        _fields_ = [("data1", u32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    class FilterData(ctypes.Union):
        _fields_ = [("class_guid", Guid), ("target", pointer),
                    ("instance_id", ctypes.c_uint16 * 200)]

    class Filter(ctypes.Structure):
        _fields_ = [("size", u32), ("flags", u32), ("kind", u32),
                    ("reserved", u32), ("data", FilterData)]

    return Filter


# Video cameras and the legacy video category; capture alone also matches audio.
_WINDOWS_CAMERA_CLASSES = (
    "e5323777-f976-4f5b-9b55-b94699c46e44",  # KSCATEGORY_VIDEO_CAMERA
    "6994ad05-93ef-11d0-a3cc-00a0c9223196",  # KSCATEGORY_VIDEO
)


class _WindowsSubscription(_Subscription):
    def __init__(self, on_change: Callable[[], None]) -> None:
        import uuid

        lib = ctypes.WinDLL("cfgmgr32", use_last_error=True)  # type: ignore[attr-defined]
        native_filter = _windows_abi()
        callback_type = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32,
        )
        lib.CM_Register_Notification.argtypes = [
            ctypes.POINTER(native_filter), ctypes.c_void_p, callback_type,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.CM_Register_Notification.restype = ctypes.c_uint32
        lib.CM_Unregister_Notification.argtypes = [ctypes.c_void_p]
        lib.CM_Unregister_Notification.restype = ctypes.c_uint32
        self._lib = lib
        self._handles: list[ctypes.c_void_p] = []
        self._native_callback = callback_type(self._event)
        super().__init__(on_change)
        try:
            for identifier in _WINDOWS_CAMERA_CLASSES:
                event_filter = native_filter()
                event_filter.size = ctypes.sizeof(native_filter)
                event_filter.data.class_guid = type(event_filter.data.class_guid).from_buffer_copy(
                    uuid.UUID(identifier).bytes_le,
                )
                handle = ctypes.c_void_p()
                result = lib.CM_Register_Notification(
                    ctypes.byref(event_filter), None, self._native_callback, ctypes.byref(handle),
                )
                if result:
                    raise OSError(f"CM_Register_Notification failed (CONFIGRET {result:#x})")
                self._handles.append(handle)
        except BaseException:  # noqa: BLE001 - release partial native registration before re-raising
            self._initialized.set()
            self.close()
            raise
        finally:
            self._initialized.set()

    def _event(self, _handle, _context, action, _data, _size) -> int:
        if action in (0, 1):  # CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL / REMOVAL
            self._notify()
        return 0  # ERROR_SUCCESS, including ignored actions

    def _release(self) -> None:
        failed = []
        for handle in self._handles:
            result = self._lib.CM_Unregister_Notification(handle)
            if result:
                failed.append(handle)
                log.error("CM_Unregister_Notification failed (CONFIGRET %#x)", result)
        self._handles = failed
        if failed:
            raise RuntimeError("Camera notification handles remain registered")
        self._native_callback = None


class _MacSubscription(_Subscription):
    def __init__(self, on_change: Callable[[], None]) -> None:
        objc = importlib.import_module("objc")
        foundation = importlib.import_module("Foundation")

        self._objc = objc
        symbols: dict[str, Any] = {}
        with objc.autorelease_pool():
            bundle = objc.loadBundle(
                "AVFoundation", symbols,
                bundle_path="/System/Library/Frameworks/AVFoundation.framework",
                scan_classes=False,
            )
            objc.loadBundleVariables(bundle, symbols, [
                ("AVCaptureDeviceWasConnectedNotification", b"@"),
                ("AVCaptureDeviceWasDisconnectedNotification", b"@"),
                ("AVMediaTypeVideo", b"@"), ("AVMediaTypeMuxed", b"@"),
                ("AVCaptureDeviceTypeBuiltInWideAngleCamera", b"@"),
                ("AVCaptureDeviceTypeExternalUnknown", b"@"),
            ], skip_undefined=False)
            self._center = foundation.NSNotificationCenter.defaultCenter()
        self._symbols = symbols
        self._tokens: list[Any] = []
        self._discovery_session = None
        super().__init__(on_change)
        try:
            with objc.autorelease_pool():
                for name in ("AVCaptureDeviceWasConnectedNotification",
                             "AVCaptureDeviceWasDisconnectedNotification"):
                    # Foundation's existing block metadata supplies the signature;
                    # queue=None delivers on the posting thread without a run loop.
                    token = self._center.addObserverForName_object_queue_usingBlock_(
                        symbols[name], None, None, self._event,
                    )
                    if token is None:
                        raise RuntimeError("Could not register AVFoundation device observer")
                    self._tokens.append(token)
                # Observers alone need not activate AVFoundation's device monitor
                # in a cold process. Retain its metadata discovery session, without
                # reading devices, requesting permission, or creating capture inputs.
                discovery = objc.lookUpClass("AVCaptureDeviceDiscoverySession")
                self._discovery_session = discovery.discoverySessionWithDeviceTypes_mediaType_position_(
                    [symbols["AVCaptureDeviceTypeBuiltInWideAngleCamera"],
                     symbols["AVCaptureDeviceTypeExternalUnknown"]], None, 0,
                )
                if self._discovery_session is None:
                    raise RuntimeError("Could not activate AVFoundation camera notifications")
        except BaseException:  # noqa: BLE001 - remove acquired observers before propagating startup failure
            self._initialized.set()
            self.close()
            raise
        finally:
            self._initialized.set()

    def _event(self, notification) -> None:
        try:
            device = notification.object()
            if device is not None and (
                device.hasMediaType_(self._symbols["AVMediaTypeVideo"])
                or device.hasMediaType_(self._symbols["AVMediaTypeMuxed"])
            ):
                self._notify()
        except Exception:  # noqa: BLE001 - Objective-C callback boundary
            log.exception("Could not handle AVFoundation camera notification")

    def _release(self) -> None:
        failed = []
        with self._objc.autorelease_pool():
            for token in self._tokens:
                try:
                    self._center.removeObserver_(token)
                except Exception:  # noqa: BLE001 - release remaining independent observers
                    failed.append(token)
                    log.exception("Could not remove AVFoundation camera observer")
            self._tokens = failed
            if not failed:
                self._discovery_session = None
        if failed:
            raise RuntimeError("AVFoundation camera observers remain registered")


def _load_udev():
    lib = ctypes.CDLL("libudev.so.1", use_errno=True)
    pointer, string, integer = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    signatures = {
        "udev_new": (pointer, []), "udev_unref": (pointer, [pointer]),
        "udev_monitor_new_from_netlink": (pointer, [pointer, string]),
        "udev_monitor_filter_add_match_subsystem_devtype": (integer, [pointer, string, string]),
        "udev_monitor_enable_receiving": (integer, [pointer]),
        "udev_monitor_get_fd": (integer, [pointer]),
        "udev_monitor_receive_device": (pointer, [pointer]),
        "udev_monitor_unref": (pointer, [pointer]),
        "udev_device_get_subsystem": (string, [pointer]),
        "udev_device_get_action": (string, [pointer]),
        "udev_device_unref": (pointer, [pointer]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    return lib


class _LinuxSubscription(_Subscription):
    def __init__(self, on_change: Callable[[], None]) -> None:
        import os
        import selectors
        import socket

        self._lib = _load_udev()
        self._context = self._lib.udev_new()
        if not self._context:
            raise OSError("udev_new failed")
        self._monitor = None
        self._reader = self._writer = None
        self._selector = None
        self._worker: threading.Thread | None = None
        super().__init__(on_change)
        try:
            self._monitor = self._lib.udev_monitor_new_from_netlink(self._context, b"udev")
            if not self._monitor:
                raise OSError("udev_monitor_new_from_netlink failed")
            result = self._lib.udev_monitor_filter_add_match_subsystem_devtype(
                self._monitor, b"video4linux", None,
            )
            if result < 0:
                raise OSError(-result, "Could not filter udev camera notifications")
            result = self._lib.udev_monitor_enable_receiving(self._monitor)
            if result < 0:
                raise OSError(-result, "Could not enable udev camera notifications")
            self._fd = self._lib.udev_monitor_get_fd(self._monitor)
            if self._fd < 0:
                raise OSError("Could not obtain udev monitor descriptor")
            os.set_blocking(self._fd, False)
            self._reader, self._writer = socket.socketpair()
            self._writer.setblocking(False)
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._fd, selectors.EVENT_READ)
            self._selector.register(self._reader, selectors.EVENT_READ, "close")
            worker = threading.Thread(target=self._run, name="camera-udev-monitor", daemon=True)
            self._worker = worker
            try:
                worker.start()
            except BaseException:  # noqa: BLE001 - do not join a monitor thread that never started
                self._worker = None
                raise
        except BaseException:  # noqa: BLE001 - release acquired native resources before re-raising
            self._initialized.set()
            self.close()
            raise
        finally:
            self._initialized.set()

    def _run(self) -> None:
        assert self._selector is not None
        try:
            while True:
                try:
                    ready = self._selector.select()
                except InterruptedError:
                    continue
                if any(key.data == "close" for key, _mask in ready):
                    return
                if not ready:
                    continue
                # Bound each drain so a device-event flood cannot starve close.
                for _ in range(32):
                    ctypes.set_errno(0)
                    device = self._lib.udev_monitor_receive_device(self._monitor)
                    if not device:
                        code = ctypes.get_errno()
                        if code not in (0, errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                            raise OSError(code, "udev monitor receive failed")
                        break
                    try:
                        if (self._lib.udev_device_get_subsystem(device) == b"video4linux"
                            and self._lib.udev_device_get_action(device)
                            in (b"add", b"remove", b"change", b"move", b"bind", b"unbind")):
                            self._notify()
                    finally:
                        self._lib.udev_device_unref(device)
        except Exception:  # noqa: BLE001 - stop failed native monitor without a retry loop
            log.exception("Camera udev monitor stopped")
            self.close()

    def _release(self) -> None:
        if self._worker is not None:
            assert self._writer is not None
            try:
                self._writer.send(b"\0")
            except BlockingIOError:
                pass  # An existing wake byte is already sufficient.
            self._worker.join()
        if self._selector is not None:
            self._selector.close()
        if self._reader is not None:
            assert self._writer is not None
            self._reader.close()
            self._writer.close()
        if self._monitor:
            self._lib.udev_monitor_unref(self._monitor)
        self._lib.udev_unref(self._context)
