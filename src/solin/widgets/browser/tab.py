from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from native_webview_widget import NativeWebView, NativeWebViewError

from .aspect_frame import AspectRatioViewFrame
from solin.ui.browser.native_adapters import (
    BrowserBridge,
    HistoryAdapter,
    NativePageAdapter,
    UrlValue,
)
from solin.core.network.browser_urls import normalize_browser_input

log = logging.getLogger(__name__)


class ProjectableWebView(NativeWebView):
    """Native WebView2/WKWebView adapter with the old BrowserWidget surface."""

    urlChanged = Signal(object)
    loadProgress = Signal(int)
    loadFinished = Signal(bool)
    new_tab_requested = Signal(str)
    download_requested = Signal(str)

    def __init__(
        self,
        *,
        session_id: str,
        session_data_root: Path,
        overlay_js: str = "",
        parent=None,
    ):
        super().__init__(
            parent,
            session_id=session_id,
            session_data_root=session_data_root,
        )
        self.bridge = BrowserBridge(self)
        self._history = HistoryAdapter(self)
        self._page = NativePageAdapter(self)
        self._current_url = "about:blank"
        self._current_title = ""
        self._projection_active = False
        self._overlay_installed = False

        self.set_download_policy(lambda _url: False)
        self.set_devtools_enabled(False)
        self.install_script_bridge()
        if overlay_js:
            self.add_document_script(overlay_js)

        self.ready.connect(self._on_ready)
        self.navigationStarted.connect(self._on_navigation_started)
        self.navigationFinished.connect(self._on_navigation_finished)
        self.navigationFailed.connect(self._on_navigation_failed)
        self.titleChanged.connect(self._on_title_changed)
        self.newWindowRequested.connect(
            lambda url: self.new_tab_requested.emit(url or "https://www.google.com")
        )
        self.downloadRequested.connect(self.download_requested)
        self.scriptMessageReceived.connect(self._on_script_message)

    def _on_ready(self) -> None:
        self._overlay_installed = True
        self._push_overlay_runtime_config()

    def _on_navigation_started(self, url: str) -> None:
        if url:
            self._current_url = url
            self.urlChanged.emit(UrlValue(url))
        self.loadProgress.emit(10)

    def _on_navigation_finished(self, url: str) -> None:
        if url:
            self._current_url = url
            self.urlChanged.emit(UrlValue(url))
        self.loadProgress.emit(100)
        self.loadFinished.emit(True)
        self._push_overlay_runtime_config()

    def _on_navigation_failed(self, _message: str) -> None:
        self.loadProgress.emit(100)
        self.loadFinished.emit(False)

    def _on_title_changed(self, title: str) -> None:
        self._current_title = title or ""

    def _on_script_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        kind = payload.get("type")
        if kind == "projectImage":
            self.bridge.project_image_signal.emit(str(payload.get("data") or ""))
        elif kind == "projectVideo":
            self.bridge.project_video_signal.emit(str(payload.get("url") or ""))
        elif kind == "cropSelected":
            self.bridge.crop_selected_signal.emit(
                float(payload.get("x") or 0),
                float(payload.get("y") or 0),
                float(payload.get("w") or 0),
                float(payload.get("h") or 0),
            )
        elif kind == "cancelCrop":
            self.bridge.crop_cancelled_signal.emit()
        elif kind == "saveMedia":
            self.bridge.save_media_signal.emit(
                str(payload.get("url") or ""),
                str(payload.get("mediaType") or payload.get("media_type") or ""),
            )
        elif kind == "addToPlaylist":
            self.bridge.add_to_playlist_signal.emit(
                str(payload.get("url") or ""),
                str(payload.get("title") or ""),
                str(payload.get("mediaType") or payload.get("media_type") or ""),
            )

    def _push_overlay_labels(self) -> None:
        lbl_proj_img = self.tr("Project image")
        lbl_proj_vid = self.tr("Project video")
        lbl_save_img = self.tr("Save image")
        lbl_save_vid = self.tr("Save video")
        lbl_add_play = self.tr("Add to playlist")
        js = (
            f"window._jwProjectLabel      = {json.dumps(lbl_proj_img)};"
            f"window._jwProjectVideoLabel = {json.dumps(lbl_proj_vid)};"
            f"window._jwSaveImageLabel    = {json.dumps(lbl_save_img)};"
            f"window._jwSaveVideoLabel    = {json.dumps(lbl_save_vid)};"
            f"window._jwAddPlaylistLabel  = {json.dumps(lbl_add_play)};"
        )
        self.run_javascript(js)

    def _push_overlay_enabled_state(self) -> None:
        enabled = not self._projection_active
        enabled_js = json.dumps(enabled)
        js = (
            f"window.__solinMediaHoverOverlaysEnabled = {enabled_js};"
            "if (window.__solinSetMediaHoverOverlaysEnabled) "
            f"window.__solinSetMediaHoverOverlaysEnabled({enabled_js});"
        )
        self.run_javascript(js)

    def _push_overlay_runtime_config(self) -> None:
        self._push_overlay_labels()
        self._push_overlay_enabled_state()

    def run_javascript(self, script: str) -> None:
        try:
            self.eval_js(script)
        except NativeWebViewError:
            pass

    def load(self, url: str) -> None:
        url = normalize_browser_input(url)
        self._current_url = url
        self.navigate(url)

    def back(self) -> None:
        self.go_back()

    def forward(self) -> None:
        self.go_forward()

    def stop(self) -> None:
        pass

    def history(self) -> HistoryAdapter:
        return self._history

    def page(self) -> NativePageAdapter:
        return self._page

    def url(self) -> UrlValue:
        return UrlValue(self._current_url)

    def title(self) -> str:
        return self._current_title

    def set_projection_active(self, active: bool) -> None:
        self._projection_active = active
        self._push_overlay_enabled_state()

    def ensure_active(self) -> None:
        try:
            self._ensure_created()
        except NativeWebViewError as exc:
            log.warning("Native view activation failed: %s", exc)

    def hideEvent(self, event):
        if self._projection_active:
            event.ignore()
            return
        super().hideEvent(event)


class BrowserTab(QWidget):
    url_changed = Signal(str)
    title_changed = Signal(str)
    load_progress = Signal(int)
    load_finished = Signal(bool)
    project_image = Signal(str)
    project_video = Signal(str)
    crop_selected = Signal(float, float, float, float)
    crop_cancelled = Signal()
    new_tab_page = Signal(object)
    save_media = Signal(str, str)
    add_to_playlist = Signal(str, str, str)
    download_requested = Signal(str)

    def __init__(
        self,
        session_id: str,
        session_data_root: Path,
        lang_manager,
        parent=None,
        *,
        overlay_js: str = "",
    ):
        super().__init__(parent)
        self.lang = lang_manager
        self._deferred_url = ""

        self._view_frame = AspectRatioViewFrame(self)
        self.view = ProjectableWebView(
            session_id=session_id,
            session_data_root=session_data_root,
            overlay_js=overlay_js,
            parent=self._view_frame,
        )
        self._view_frame.set_child(self.view)
        self._connect_view()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view_frame)

    def set_browser_aspect_ratio_lock(self, active: bool, ratio: float) -> None:
        self._view_frame.set_aspect_ratio_lock(active, ratio)

    def _connect_view(self):
        bridge = self.view.bridge
        bridge.project_image_signal.connect(self.project_image)
        bridge.project_video_signal.connect(self.project_video)
        bridge.crop_selected_signal.connect(self.crop_selected)
        bridge.crop_cancelled_signal.connect(self.crop_cancelled)
        bridge.save_media_signal.connect(self.save_media)
        bridge.add_to_playlist_signal.connect(self.add_to_playlist)

        self.view.new_tab_requested.connect(self._on_new_tab)
        self.view.download_requested.connect(self.download_requested)
        self.view.urlChanged.connect(lambda u: self.url_changed.emit(u.toString()))
        self.view.titleChanged.connect(self.title_changed)
        self.view.loadProgress.connect(self.load_progress)
        self.view.loadFinished.connect(self.load_finished)
        self.view.urlChanged.connect(lambda _: self.view._push_overlay_runtime_config())

    def _on_new_tab(self, url: str):
        self.new_tab_page.emit(url)

    def _push_labels(self):
        self.view._push_overlay_runtime_config()

    def adopt_page(self, page):
        if isinstance(page, str):
            self.load(page)

    def load(self, url: str):
        self.view.load(url)

    @property
    def current_url(self) -> str:
        return self.view.url().toString()
