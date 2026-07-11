from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .abi import (
    EVENT_DOWNLOAD_REQUESTED,
    EVENT_NAVIGATION_FAILED,
    EVENT_NAVIGATION_FINISHED,
    EVENT_NAVIGATION_STARTED,
    EVENT_NEW_WINDOW_REQUESTED,
    EVENT_READY,
    EVENT_SCRIPT_MESSAGE,
    EVENT_TITLE_CHANGED,
    EVENT_ZOOM_FACTOR_CHANGED,
    native_library_candidates,
)


class NativeWebViewError(RuntimeError):
    """Raised when the native webview backend cannot be loaded or used."""


EventCallback = Callable[[int, str], None]
PolicyCallback = Callable[[int, str], bool]
CaptureCallback = Callable[[int, bool, bytes, str], None]


@dataclass(slots=True)
class NativeOptions:
    user_data_folder: str | None = None
    runtime_path: str | None = None
    session_id: str | None = None
    transparent: bool = False


class _NativeOptionsW(ctypes.Structure):
    _fields_ = [
        ("user_data_folder", ctypes.c_void_p),
        ("runtime_path", ctypes.c_void_p),
        ("session_id", ctypes.c_void_p),
        ("transparent", ctypes.c_int),
    ]


class _NativeOptionsUtf8(ctypes.Structure):
    _fields_ = [
        ("user_data_folder", ctypes.c_void_p),
        ("runtime_path", ctypes.c_void_p),
        ("session_id", ctypes.c_void_p),
        ("transparent", ctypes.c_int),
    ]


class NativeCookie:
    def __init__(
        self,
        *,
        name: str,
        value: str,
        domain: str,
        path: str = "/",
        expires: float = 0,
        secure: bool = False,
        http_only: bool = False,
        same_site: str = "lax",
    ) -> None:
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.expires = expires
        self.secure = secure
        self.http_only = http_only
        self.same_site = same_site


class _NativeCookieW(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_void_p),
        ("value", ctypes.c_void_p),
        ("domain", ctypes.c_void_p),
        ("path", ctypes.c_void_p),
        ("expires", ctypes.c_double),
        ("secure", ctypes.c_int),
        ("http_only", ctypes.c_int),
        ("same_site", ctypes.c_int),
    ]


class _NativeCookieUtf8(ctypes.Structure):
    _fields_ = _NativeCookieW._fields_


class NativeBackend:
    EVENT_READY = EVENT_READY
    EVENT_NAVIGATION_STARTED = EVENT_NAVIGATION_STARTED
    EVENT_NAVIGATION_FINISHED = EVENT_NAVIGATION_FINISHED
    EVENT_NAVIGATION_FAILED = EVENT_NAVIGATION_FAILED
    EVENT_TITLE_CHANGED = EVENT_TITLE_CHANGED
    EVENT_DOWNLOAD_REQUESTED = EVENT_DOWNLOAD_REQUESTED
    EVENT_NEW_WINDOW_REQUESTED = EVENT_NEW_WINDOW_REQUESTED
    EVENT_SCRIPT_MESSAGE = EVENT_SCRIPT_MESSAGE
    EVENT_ZOOM_FACTOR_CHANGED = EVENT_ZOOM_FACTOR_CHANGED

    def __init__(self) -> None:
        self._system = platform.system()
        library_path = self._resolve_library()
        try:
            self._lib = ctypes.CDLL(str(library_path))
        except OSError as exc:
            hint = ""
            if self._system == "Linux":
                hint = " Install the WebKitGTK 4.1 and GTK 3 runtime libraries."
            raise NativeWebViewError(
                f"Failed to load native webview library {library_path}: {exc}.{hint}"
            ) from exc
        self._callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        self._policy_callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        self._capture_callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.c_void_p,
        )
        self._callbacks: dict[int, Any] = {}
        self._policy_callbacks: dict[int, Any] = {}
        self._capture_callbacks: dict[int, Any] = {}
        self._zoom_supported = all(
            hasattr(self._lib, name)
            for name in ("nwv_set_zoom_factor", "nwv_get_zoom_factor")
        )
        self._configure_signatures()

    @property
    def zoom_supported(self) -> bool:
        return self._zoom_supported

    @property
    def uses_foreign_window(self) -> bool:
        return self._system == "Linux"

    def create(self, parent_handle: int, options: NativeOptions, callback: EventCallback) -> int:
        native_options, keepalive = self._build_options(options)
        handle = self._lib.nwv_create(ctypes.c_void_p(parent_handle), ctypes.byref(native_options))
        if not handle:
            raise NativeWebViewError("Native webview creation failed.")

        def trampoline(_user_data: int, event_type: int, message_ptr: int) -> None:
            callback(event_type, self._decode_message(message_ptr))

        c_callback = self._callback_type(trampoline)
        self._callbacks[int(handle)] = c_callback
        self._lib.nwv_set_event_callback(handle, c_callback, None)
        self._keepalive = keepalive
        return int(handle)

    def set_policy_callback(self, handle: int, callback: PolicyCallback | None) -> None:
        if not handle:
            return

        if callback is None:
            self._policy_callbacks.pop(int(handle), None)
            self._lib.nwv_set_policy_callback(ctypes.c_void_p(handle), self._policy_callback_type(), None)
            return

        def trampoline(_user_data: int, event_type: int, message_ptr: int) -> int:
            return 1 if callback(event_type, self._decode_message(message_ptr)) else 0

        c_callback = self._policy_callback_type(trampoline)
        self._policy_callbacks[int(handle)] = c_callback
        self._lib.nwv_set_policy_callback(ctypes.c_void_p(handle), c_callback, None)

    def set_capture_callback(self, handle: int, callback: CaptureCallback | None) -> None:
        if not handle:
            return

        if callback is None:
            self._capture_callbacks.pop(int(handle), None)
            self._lib.nwv_set_capture_callback(ctypes.c_void_p(handle), self._capture_callback_type(), None)
            return

        def trampoline(
            _user_data: int,
            request_id: int,
            success: int,
            data_ptr: Any,
            size: int,
            error_ptr: int,
        ) -> None:
            data = ctypes.string_at(data_ptr, size) if data_ptr and size else b""
            callback(request_id, bool(success), data, self._decode_message(error_ptr))

        c_callback = self._capture_callback_type(trampoline)
        self._capture_callbacks[int(handle)] = c_callback
        self._lib.nwv_set_capture_callback(ctypes.c_void_p(handle), c_callback, None)

    def destroy(self, handle: int) -> None:
        if not handle:
            return
        self._callbacks.pop(int(handle), None)
        self._policy_callbacks.pop(int(handle), None)
        self._capture_callbacks.pop(int(handle), None)
        self._lib.nwv_destroy(ctypes.c_void_p(handle))

    def native_view(self, handle: int) -> int:
        if not self.uses_foreign_window or not handle:
            return 0
        return int(self._lib.nwv_get_native_view(ctypes.c_void_p(handle)))

    def resize(self, handle: int, width: int, height: int) -> None:
        if handle:
            self._lib.nwv_resize(ctypes.c_void_p(handle), int(width), int(height))

    def navigate(self, handle: int, url: str) -> bool:
        return self._call_text("nwv_navigate", handle, url)

    def set_html(self, handle: int, html: str, base_url: str | None = None) -> bool:
        if self._system == "Windows":
            html_value = ctypes.c_wchar_p(html)
            base_value = ctypes.c_wchar_p(base_url) if base_url else None
        else:
            html_value = ctypes.c_char_p(html.encode("utf-8"))
            base_value = ctypes.c_char_p(base_url.encode("utf-8")) if base_url else None
        result = self._lib.nwv_set_html(
            ctypes.c_void_p(handle),
            ctypes.cast(html_value, ctypes.c_void_p),
            ctypes.cast(base_value, ctypes.c_void_p) if base_value else None,
        )
        return bool(result)

    def reload(self, handle: int) -> bool:
        return bool(self._lib.nwv_reload(ctypes.c_void_p(handle)))

    def go_back(self, handle: int) -> bool:
        return bool(self._lib.nwv_go_back(ctypes.c_void_p(handle)))

    def go_forward(self, handle: int) -> bool:
        return bool(self._lib.nwv_go_forward(ctypes.c_void_p(handle)))

    def eval_js(self, handle: int, script: str) -> bool:
        return self._call_text("nwv_eval_js", handle, script)

    def add_document_script(self, handle: int, script: str) -> bool:
        return self._call_text("nwv_add_document_script", handle, script)

    def set_default_context_menu_enabled(self, handle: int, enabled: bool) -> bool:
        result = self._lib.nwv_set_default_context_menu_enabled(ctypes.c_void_p(handle), int(enabled))
        return bool(result)

    def set_devtools_enabled(self, handle: int, enabled: bool) -> bool:
        result = self._lib.nwv_set_devtools_enabled(ctypes.c_void_p(handle), int(enabled))
        return bool(result)

    def capture_png(self, handle: int, request_id: int, x: int, y: int, width: int, height: int) -> bool:
        result = self._lib.nwv_capture_png(
            ctypes.c_void_p(handle),
            int(request_id),
            int(x),
            int(y),
            int(width),
            int(height),
        )
        return bool(result)

    def capture_jpeg(self, handle: int, request_id: int) -> bool:
        result = self._lib.nwv_capture_jpeg(ctypes.c_void_p(handle), int(request_id))
        return bool(result)

    def start_frame_stream(
        self,
        handle: int,
        quality: int,
        max_width: int,
        max_height: int,
        every_nth_frame: int,
    ) -> bool:
        result = self._lib.nwv_start_frame_stream(
            ctypes.c_void_p(handle),
            int(quality),
            int(max_width),
            int(max_height),
            int(every_nth_frame),
        )
        return bool(result)

    def stop_frame_stream(self, handle: int) -> bool:
        result = self._lib.nwv_stop_frame_stream(ctypes.c_void_p(handle))
        return bool(result)

    def set_cookie(self, handle: int, cookie: NativeCookie) -> bool:
        native_cookie, _keepalive = self._build_cookie(cookie)
        result = self._lib.nwv_set_cookie(ctypes.c_void_p(handle), ctypes.byref(native_cookie))
        return bool(result)

    def clear_cookies(self, handle: int) -> bool:
        return bool(self._lib.nwv_clear_cookies(ctypes.c_void_p(handle)))

    def can_go_back(self, handle: int) -> bool:
        return bool(self._lib.nwv_can_go_back(ctypes.c_void_p(handle)))

    def can_go_forward(self, handle: int) -> bool:
        return bool(self._lib.nwv_can_go_forward(ctypes.c_void_p(handle)))

    def set_zoom_factor(self, handle: int, factor: float) -> bool:
        if not self._zoom_supported:
            return False
        return bool(
            self._lib.nwv_set_zoom_factor(
                ctypes.c_void_p(handle),
                ctypes.c_double(factor),
            )
        )

    def get_zoom_factor(self, handle: int) -> float:
        if not self._zoom_supported:
            return 0.0
        return float(self._lib.nwv_get_zoom_factor(ctypes.c_void_p(handle)))

    def _configure_signatures(self) -> None:
        self._lib.nwv_create.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nwv_create.restype = ctypes.c_void_p
        if self.uses_foreign_window:
            self._lib.nwv_get_native_view.argtypes = [ctypes.c_void_p]
            self._lib.nwv_get_native_view.restype = ctypes.c_size_t
        self._lib.nwv_destroy.argtypes = [ctypes.c_void_p]
        self._lib.nwv_set_event_callback.argtypes = [ctypes.c_void_p, self._callback_type, ctypes.c_void_p]
        self._lib.nwv_set_policy_callback.argtypes = [ctypes.c_void_p, self._policy_callback_type, ctypes.c_void_p]
        self._lib.nwv_set_capture_callback.argtypes = [
            ctypes.c_void_p,
            self._capture_callback_type,
            ctypes.c_void_p,
        ]
        self._lib.nwv_resize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self._lib.nwv_navigate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nwv_set_html.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nwv_eval_js.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nwv_add_document_script.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.nwv_set_default_context_menu_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._lib.nwv_set_devtools_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._lib.nwv_capture_png.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._lib.nwv_capture_jpeg.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._lib.nwv_start_frame_stream.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._lib.nwv_stop_frame_stream.argtypes = [ctypes.c_void_p]
        self._lib.nwv_set_cookie.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if self._zoom_supported:
            self._lib.nwv_set_zoom_factor.argtypes = [ctypes.c_void_p, ctypes.c_double]
            self._lib.nwv_set_zoom_factor.restype = ctypes.c_int
            self._lib.nwv_get_zoom_factor.argtypes = [ctypes.c_void_p]
            self._lib.nwv_get_zoom_factor.restype = ctypes.c_double

        for name in (
            "nwv_navigate",
            "nwv_set_html",
            "nwv_reload",
            "nwv_go_back",
            "nwv_go_forward",
            "nwv_eval_js",
            "nwv_add_document_script",
            "nwv_set_default_context_menu_enabled",
            "nwv_set_devtools_enabled",
            "nwv_capture_png",
            "nwv_capture_jpeg",
            "nwv_start_frame_stream",
            "nwv_stop_frame_stream",
            "nwv_set_cookie",
            "nwv_clear_cookies",
            "nwv_can_go_back",
            "nwv_can_go_forward",
        ):
            getattr(self._lib, name).restype = ctypes.c_int

    def _resolve_library(self) -> Path:
        configured = os.environ.get("NATIVE_WEBVIEW_WIDGET_LIB")
        if configured:
            path = Path(configured)
            if path.exists():
                return path
            raise NativeWebViewError(f"NATIVE_WEBVIEW_WIDGET_LIB points to a missing file: {path}")

        package_dir = Path(__file__).resolve().parent
        candidates = native_library_candidates(self._system, package_dir)
        if not candidates:
            raise NativeWebViewError(
                "native-webview-widget supports Windows, macOS, and Linux only."
            )

        for candidate in candidates:
            if candidate.exists():
                return candidate

        names = ", ".join(str(candidate) for candidate in candidates)
        raise NativeWebViewError(
            "Native webview library was not found. Build the native backend and place it at "
            f"{names}, or set NATIVE_WEBVIEW_WIDGET_LIB."
        )

    def _build_options(self, options: NativeOptions) -> tuple[ctypes.Structure, list[object]]:
        keepalive: list[object] = []
        if self._system == "Windows":
            user_data = ctypes.c_wchar_p(options.user_data_folder) if options.user_data_folder else None
            runtime_path = ctypes.c_wchar_p(options.runtime_path) if options.runtime_path else None
            session_id = ctypes.c_wchar_p(options.session_id) if options.session_id else None
            keepalive.extend(value for value in (user_data, runtime_path, session_id) if value is not None)
            native = _NativeOptionsW(
                ctypes.cast(user_data, ctypes.c_void_p).value if user_data else None,
                ctypes.cast(runtime_path, ctypes.c_void_p).value if runtime_path else None,
                ctypes.cast(session_id, ctypes.c_void_p).value if session_id else None,
                int(options.transparent),
            )
        else:
            user_data = (
                ctypes.c_char_p(options.user_data_folder.encode("utf-8"))
                if options.user_data_folder
                else None
            )
            runtime_path = (
                ctypes.c_char_p(options.runtime_path.encode("utf-8"))
                if options.runtime_path
                else None
            )
            session_id = (
                ctypes.c_char_p(options.session_id.encode("utf-8"))
                if options.session_id
                else None
            )
            keepalive.extend(value for value in (user_data, runtime_path, session_id) if value is not None)
            native = _NativeOptionsUtf8(
                ctypes.cast(user_data, ctypes.c_void_p).value if user_data else None,
                ctypes.cast(runtime_path, ctypes.c_void_p).value if runtime_path else None,
                ctypes.cast(session_id, ctypes.c_void_p).value if session_id else None,
                int(options.transparent),
            )
        return native, keepalive

    def _build_cookie(self, cookie: NativeCookie) -> tuple[ctypes.Structure, list[object]]:
        same_site_map = {"none": 0, "lax": 1, "strict": 2}
        same_site = same_site_map.get(cookie.same_site.lower(), 1)
        keepalive: list[object] = []

        if self._system == "Windows":
            values = [
                ctypes.c_wchar_p(cookie.name),
                ctypes.c_wchar_p(cookie.value),
                ctypes.c_wchar_p(cookie.domain),
                ctypes.c_wchar_p(cookie.path or "/"),
            ]
            keepalive.extend(values)
            args: list[Any] = [ctypes.cast(value, ctypes.c_void_p).value for value in values]
            args.extend([float(cookie.expires or 0), int(cookie.secure), int(cookie.http_only), same_site])
            native = _NativeCookieW(*args)
        else:
            values = [
                ctypes.c_char_p(cookie.name.encode("utf-8")),
                ctypes.c_char_p(cookie.value.encode("utf-8")),
                ctypes.c_char_p(cookie.domain.encode("utf-8")),
                ctypes.c_char_p((cookie.path or "/").encode("utf-8")),
            ]
            keepalive.extend(values)
            args: list[Any] = [ctypes.cast(value, ctypes.c_void_p).value for value in values]
            args.extend([float(cookie.expires or 0), int(cookie.secure), int(cookie.http_only), same_site])
            native = _NativeCookieUtf8(*args)

        return native, keepalive

    def _decode_message(self, message_ptr: int) -> str:
        if not message_ptr:
            return ""
        if self._system == "Windows":
            return ctypes.wstring_at(message_ptr)
        return ctypes.string_at(message_ptr).decode("utf-8", errors="replace")

    def _call_text(self, function_name: str, handle: int, text: str) -> bool:
        if self._system == "Windows":
            value = ctypes.c_wchar_p(text)
        else:
            value = ctypes.c_char_p(text.encode("utf-8"))
        result = getattr(self._lib, function_name)(
            ctypes.c_void_p(handle),
            ctypes.cast(value, ctypes.c_void_p),
        )
        return bool(result)
