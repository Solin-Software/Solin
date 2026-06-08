from __future__ import annotations

import json
from typing import Callable
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from PySide6 import QtCore, QtGui, QtWidgets

from ._backend import NativeBackend, NativeCookie, NativeOptions, NativeWebViewError


DownloadPolicy = Callable[[str], bool]


class _EventBridge(QtCore.QObject):
    received = QtCore.Signal(int, str)
    captureReceived = QtCore.Signal(int, bool, bytes, str)


class NativeWebView(QtWidgets.QWidget):
    ready = QtCore.Signal()
    navigationStarted = QtCore.Signal(str)
    navigationFinished = QtCore.Signal(str)
    navigationFailed = QtCore.Signal(str)
    titleChanged = QtCore.Signal(str)
    downloadRequested = QtCore.Signal(str)
    newWindowRequested = QtCore.Signal(str)
    scriptMessageReceived = QtCore.Signal(str)
    contextMenuRequested = QtCore.Signal(dict)
    captureCompleted = QtCore.Signal(int, bytes)
    captureFailed = QtCore.Signal(int, str)
    frameStreamFrame = QtCore.Signal(bytes)
    frameStreamFailed = QtCore.Signal(str)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        url: str | None = None,
        html: str | None = None,
        session_id: str | None = None,
        session_data_root: str | Path | None = None,
        user_data_folder: str | None = None,
        runtime_path: str | None = None,
        transparent: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontCreateNativeAncestors, False)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._backend = NativeBackend()
        self._handle = 0
        self._created = False
        self._native_ready = False
        self._create_pending = False
        self._pending_url = url
        self._pending_html = html
        self._pending_base_url: str | None = None
        self._pending_cookies: list[NativeCookie] = []
        self._pending_clear_cookies = False
        self._pending_document_scripts: list[str] = []
        self._pending_default_context_menu_enabled: bool | None = None
        self._pending_devtools_enabled: bool | None = None
        self._next_capture_request_id = 1
        self._download_policy: DownloadPolicy | None = None
        resolved_session_id = session_id or "default"
        self._options = NativeOptions(
            user_data_folder=user_data_folder
            or self._session_data_folder(resolved_session_id, session_data_root),
            runtime_path=runtime_path,
            session_id=str(uuid5(NAMESPACE_URL, f"native-webview-widget:{resolved_session_id}")),
            transparent=transparent,
        )
        self._bridge = _EventBridge(self)
        self._bridge.received.connect(self._handle_native_event, QtCore.Qt.ConnectionType.QueuedConnection)
        self._bridge.captureReceived.connect(self._handle_capture_event, QtCore.Qt.ConnectionType.QueuedConnection)

    def navigate(self, url: str) -> None:
        if not self._created or not self._native_ready:
            self._pending_url = url
            self._pending_html = None
            return
        if not self._backend.navigate(self._handle, url):
            raise NativeWebViewError(f"Failed to navigate to {url!r}.")

    def set_html(self, html: str, base_url: str | None = None) -> None:
        if not self._created or not self._native_ready:
            self._pending_html = html
            self._pending_url = None
            self._pending_base_url = base_url
            return
        if not self._backend.set_html(self._handle, html, base_url):
            raise NativeWebViewError("Failed to set HTML content.")

    def reload(self) -> None:
        self._require_created()
        self._backend.reload(self._handle)

    def go_back(self) -> None:
        self._require_created()
        self._backend.go_back(self._handle)

    def go_forward(self) -> None:
        self._require_created()
        self._backend.go_forward(self._handle)

    def eval_js(self, script: str) -> None:
        self._require_created()
        if not self._backend.eval_js(self._handle, script):
            raise NativeWebViewError("Failed to evaluate JavaScript.")

    def add_document_script(self, script: str) -> None:
        """Inject JavaScript at document creation for every future navigation."""
        if not self._created or not self._native_ready:
            self._pending_document_scripts.append(script)
            return
        if not self._backend.add_document_script(self._handle, script):
            raise NativeWebViewError("Failed to add document script.")

    def install_script_bridge(self) -> None:
        """Expose window.nativeWebView.postMessage(message) to page scripts."""
        script = """
(() => {
  if (window.nativeWebView && window.nativeWebView.postMessage) return;
  window.nativeWebView = {
    postMessage(message) {
      const value = typeof message === "string" ? message : JSON.stringify(message);
      if (window.chrome && window.chrome.webview) {
        window.chrome.webview.postMessage(value);
      } else if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.nativeWebView) {
        window.webkit.messageHandlers.nativeWebView.postMessage(value);
      }
    }
  };
})();
        """.strip()
        self.add_document_script(script)
        if self._created and self._native_ready:
            self.eval_js(script)

    def install_context_menu_bridge(self) -> None:
        """Disable the native context menu and emit contextMenuRequested payloads."""
        self.set_default_context_menu_enabled(False)
        self.install_script_bridge()
        script = """
document.addEventListener("contextmenu", function(event) {
  event.preventDefault();
  const target = event.target;
  const anchor = target && target.closest ? target.closest("a[href]") : null;
  const image = target && target.closest ? target.closest("img[src]") : null;
  window.nativeWebView.postMessage({
    type: "contextmenu",
    x: event.clientX,
    y: event.clientY,
    href: anchor ? anchor.href : "",
    src: image ? (image.currentSrc || image.src || "") : "",
    text: target && target.innerText ? target.innerText.slice(0, 500) : ""
  });
}, true);
        """.strip()
        self.add_document_script(script)
        if self._created and self._native_ready:
            self.eval_js(script)

    def set_default_context_menu_enabled(self, enabled: bool) -> None:
        if not self._created or not self._native_ready:
            self._pending_default_context_menu_enabled = enabled
            return
        if not self._backend.set_default_context_menu_enabled(self._handle, enabled):
            raise NativeWebViewError("Failed to update native context menu setting.")

    def set_devtools_enabled(self, enabled: bool) -> None:
        if not self._created or not self._native_ready:
            self._pending_devtools_enabled = enabled
            return
        if not self._backend.set_devtools_enabled(self._handle, enabled):
            raise NativeWebViewError("Failed to update devtools setting.")

    def capture_frame(self) -> int:
        """Capture the visible webview viewport as PNG bytes.

        Returns a request id. Listen to captureCompleted(request_id, bytes)
        or captureFailed(request_id, error).
        """
        return self._capture_png(0, 0, 0, 0)

    def capture_frame_jpeg(self) -> int:
        """Capture the visible webview viewport as JPEG bytes.

        This is intended for live projection where throughput matters more
        than lossless still-image quality.
        """
        self._require_created()
        if not self._native_ready:
            raise NativeWebViewError("Native webview is not ready for capture yet.")

        request_id = self._next_capture_request_id
        self._next_capture_request_id += 1
        if not self._backend.capture_jpeg(self._handle, request_id):
            raise NativeWebViewError("Failed to start native JPEG capture.")
        return request_id

    def capture_region(self, x: int, y: int, width: int, height: int) -> int:
        """Capture a viewport-relative region as PNG bytes.

        Coordinates are in widget pixels, using a top-left origin.
        """
        if width <= 0 or height <= 0:
            raise ValueError("Capture region width and height must be greater than zero.")
        return self._capture_png(x, y, width, height)

    def start_frame_stream(
        self,
        *,
        quality: int = 75,
        max_width: int = 0,
        max_height: int = 0,
        every_nth_frame: int = 1,
    ) -> bool:
        """Start a native JPEG frame stream when the platform supports it.

        Frames are emitted through frameStreamFrame(bytes). On unsupported
        platforms this returns False so callers can use capture_frame_jpeg().
        """
        self._require_created()
        if not self._native_ready:
            raise NativeWebViewError("Native webview is not ready for frame streaming yet.")

        return self._backend.start_frame_stream(
            self._handle,
            int(quality),
            int(max_width),
            int(max_height),
            int(every_nth_frame),
        )

    def stop_frame_stream(self) -> None:
        if self._handle:
            self._backend.stop_frame_stream(self._handle)

    def set_download_policy(self, callback: DownloadPolicy | None) -> None:
        """Set a synchronous whitelist callback for native downloads.

        Return True to allow the browser engine to continue the download.
        Return False to cancel it so the application can handle the URL itself.
        """
        self._download_policy = callback
        if self._created:
            self._backend.set_policy_callback(self._handle, self._handle_policy_request)

    def set_cookie(
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
        cookie = NativeCookie(
            name=name,
            value=value,
            domain=domain,
            path=path,
            expires=expires,
            secure=secure,
            http_only=http_only,
            same_site=same_site,
        )
        if not self._created or not self._native_ready:
            self._pending_cookies.append(cookie)
            return
        if not self._backend.set_cookie(self._handle, cookie):
            raise NativeWebViewError(f"Failed to set cookie {name!r} for {domain!r}.")

    def clear_cookies(self) -> None:
        self._pending_cookies.clear()
        if not self._created or not self._native_ready:
            self._pending_clear_cookies = True
            return
        self._require_created()
        if not self._backend.clear_cookies(self._handle):
            raise NativeWebViewError("Failed to clear cookies.")

    def can_go_back(self) -> bool:
        return self._created and self._backend.can_go_back(self._handle)

    def can_go_forward(self) -> bool:
        return self._created and self._backend.can_go_forward(self._handle)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # type: ignore[name-defined]
        super().showEvent(event)
        self._schedule_ensure_created()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # type: ignore[name-defined]
        super().resizeEvent(event)
        if self._created:
            size = event.size()
            self._backend.resize(self._handle, size.width(), size.height())
        elif self.isVisible():
            self._schedule_ensure_created()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[name-defined]
        self.dispose()
        super().closeEvent(event)

    def dispose(self) -> None:
        if self._handle:
            self._backend.stop_frame_stream(self._handle)
            self._backend.destroy(self._handle)
        self._handle = 0
        self._created = False
        self._native_ready = False
        self._create_pending = False

    def _schedule_ensure_created(self) -> None:
        if self._created or self._create_pending:
            return
        self._create_pending = True
        QtCore.QTimer.singleShot(0, self._ensure_created_if_ready)

    def _ensure_created_if_ready(self) -> None:
        self._create_pending = False
        if self._created or not self.isVisible():
            return
        if self.width() <= 0 or self.height() <= 0:
            return
        self._ensure_created()

    def _ensure_created(self) -> None:
        if self._created:
            return

        self._create_pending = False
        self.ensurePolished()
        width = max(1, self.width())
        height = max(1, self.height())
        parent_handle = int(self.winId())
        self._handle = self._backend.create(parent_handle, self._options, self._emit_native_event)
        self._created = True
        self._backend.set_policy_callback(self._handle, self._handle_policy_request)
        self._backend.set_capture_callback(self._handle, self._emit_capture_event)
        self._backend.resize(self._handle, width, height)

    def _require_created(self) -> None:
        if not self._created:
            self._ensure_created()

    def _emit_native_event(self, event_type: int, message: str) -> None:
        self._bridge.received.emit(event_type, message)

    def _emit_capture_event(self, request_id: int, success: bool, data: bytes, error: str) -> None:
        self._bridge.captureReceived.emit(request_id, success, data, error)

    def _handle_native_event(self, event_type: int, message: str) -> None:
        if event_type == NativeBackend.EVENT_READY:
            self._native_ready = True
            self.ready.emit()
            self._flush_pending_settings()
            self._flush_pending_document_scripts()
            self._flush_pending_clear_cookies()
            self._flush_pending_cookies()
            self._flush_pending_load()
        elif event_type == NativeBackend.EVENT_NAVIGATION_STARTED:
            self.navigationStarted.emit(message)
        elif event_type == NativeBackend.EVENT_NAVIGATION_FINISHED:
            self.navigationFinished.emit(message)
        elif event_type == NativeBackend.EVENT_NAVIGATION_FAILED:
            self.navigationFailed.emit(message)
        elif event_type == NativeBackend.EVENT_TITLE_CHANGED:
            self.titleChanged.emit(message)
        elif event_type == NativeBackend.EVENT_DOWNLOAD_REQUESTED:
            self.downloadRequested.emit(message)
        elif event_type == NativeBackend.EVENT_NEW_WINDOW_REQUESTED:
            self.newWindowRequested.emit(message)
        elif event_type == NativeBackend.EVENT_SCRIPT_MESSAGE:
            self.scriptMessageReceived.emit(message)
            self._handle_script_message(message)

    def _handle_policy_request(self, event_type: int, message: str) -> bool:
        if event_type == NativeBackend.EVENT_DOWNLOAD_REQUESTED:
            return bool(self._download_policy and self._download_policy(message))
        return False

    def _flush_pending_load(self) -> None:
        if self._pending_html is not None:
            html = self._pending_html
            base_url = self._pending_base_url
            self._pending_html = None
            self._pending_base_url = None
            if not self._backend.set_html(self._handle, html, base_url):
                self.navigationFailed.emit("Failed to set pending HTML content.")
        elif self._pending_url is not None:
            url = self._pending_url
            self._pending_url = None
            if not self._backend.navigate(self._handle, url):
                self.navigationFailed.emit(f"Failed to navigate to {url!r}.")

    def _flush_pending_cookies(self) -> None:
        pending = self._pending_cookies
        self._pending_cookies = []
        for cookie in pending:
            if not self._backend.set_cookie(self._handle, cookie):
                self.navigationFailed.emit(f"Failed to set pending cookie {cookie.name!r}.")

    def _flush_pending_clear_cookies(self) -> None:
        if not self._pending_clear_cookies:
            return
        self._pending_clear_cookies = False
        if not self._backend.clear_cookies(self._handle):
            self.navigationFailed.emit("Failed to clear pending cookies.")

    def _flush_pending_document_scripts(self) -> None:
        pending = self._pending_document_scripts
        self._pending_document_scripts = []
        for script in pending:
            if not self._backend.add_document_script(self._handle, script):
                self.navigationFailed.emit("Failed to add pending document script.")

    def _flush_pending_settings(self) -> None:
        if self._pending_default_context_menu_enabled is not None:
            enabled = self._pending_default_context_menu_enabled
            self._pending_default_context_menu_enabled = None
            if not self._backend.set_default_context_menu_enabled(self._handle, enabled):
                self.navigationFailed.emit("Failed to apply context menu setting.")
        if self._pending_devtools_enabled is not None:
            enabled = self._pending_devtools_enabled
            self._pending_devtools_enabled = None
            if not self._backend.set_devtools_enabled(self._handle, enabled):
                self.navigationFailed.emit("Failed to apply devtools setting.")

    def _handle_script_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and payload.get("type") == "contextmenu":
            self.contextMenuRequested.emit(payload)

    def _capture_png(self, x: int, y: int, width: int, height: int) -> int:
        self._require_created()
        if not self._native_ready:
            raise NativeWebViewError("Native webview is not ready for capture yet.")

        request_id = self._next_capture_request_id
        self._next_capture_request_id += 1
        if not self._backend.capture_png(self._handle, request_id, x, y, width, height):
            raise NativeWebViewError("Failed to start native PNG capture.")
        return request_id

    def _handle_capture_event(self, request_id: int, success: bool, data: bytes, error: str) -> None:
        if request_id == 0:
            if success:
                self.frameStreamFrame.emit(data)
            else:
                self.frameStreamFailed.emit(error or "Native frame stream failed.")
            return

        if success:
            self.captureCompleted.emit(request_id, data)
        else:
            self.captureFailed.emit(request_id, error or "Native capture failed.")

    @staticmethod
    def _session_data_folder(session_id: str, session_data_root: str | Path | None) -> str:
        root = Path(session_data_root) if session_data_root else Path.home() / ".native-webview-widget"
        safe_session = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in session_id)
        return str(root / "sessions" / safe_session)
