from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer

from .tab import BrowserTab
from solin.core.network.browser_urls import normalize_browser_input


__all__ = ("BrowserNavigationMixin",)


class BrowserNavigationMixin:
    def _new_tab(self, url: str = "", page=None, focus: bool = True) -> "BrowserTab":
        if isinstance(page, str) and not url:
            url = page

        tab = BrowserTab(
            self._session_id,
            self._session_data_root,
            self.lang,
            self,
            overlay_js=self._OVERLAY_JS,
        )

        initial_url = url or "https://www.google.com"
        if focus:
            tab.load(initial_url)
        else:
            tab._deferred_url = initial_url

        tab.url_changed.connect(lambda u, t=tab: self._on_url_changed(t, u))
        tab.title_changed.connect(lambda ttl, t=tab: self._update_tab_title(t, ttl))
        tab.load_progress.connect(self._on_load_progress)
        tab.load_finished.connect(lambda _, t=tab: self._on_load_finished(t))
        tab.project_image.connect(self._on_project_image)
        tab.project_video.connect(self._on_project_video)
        tab.crop_selected.connect(self._on_crop_selected)
        tab.crop_cancelled.connect(self._on_crop_cancelled)
        tab.new_tab_page.connect(lambda u: self._new_tab(url=u))
        tab.save_media.connect(self._on_save_media)
        tab.add_to_playlist.connect(self._on_add_to_playlist)
        tab.download_requested.connect(self._on_download_requested)
        tab.view.captureCompleted.connect(
            lambda request_id, data, t=tab: self._on_native_capture_completed(
                t, request_id, data
            )
        )
        tab.view.captureFailed.connect(
            lambda request_id, error, t=tab: self._on_native_capture_failed(
                t, request_id, error
            )
        )
        tab.view.frameStreamFrame.connect(
            lambda data, t=tab: self._on_native_frame_stream_frame(t, data)
        )
        tab.view.frameStreamFailed.connect(
            lambda error, t=tab: self._on_native_frame_stream_failed(t, error)
        )
        tab.set_browser_aspect_16_9(self._browser_aspect_16_9_active)

        label = self.tr("New tab")
        idx = self._tab_bar.addTab(label)
        self._stack.insertWidget(idx, tab)

        if focus:
            self._tab_bar.setCurrentIndex(idx)
            self._stack.setCurrentIndex(idx)
        return tab

    def _close_tab(self, index: int):
        if self._stack.widget(index) is self._pinned_tab:
            self._stop_tab_projection_internal()
        if self._tab_bar.count() == 1:
            self._navigate_to_wol()
            return
        w = self._stack.widget(index)
        self._stack.removeWidget(w)
        self._tab_bar.removeTab(index)
        if w:
            w.deleteLater()

    def _on_tab_moved(self, from_idx: int, to_idx: int):
        w = self._stack.widget(from_idx)
        self._stack.removeWidget(w)
        self._stack.insertWidget(to_idx, w)

    def _current_tab(self) -> "BrowserTab | None":
        w = self._stack.currentWidget()
        return w if isinstance(w, BrowserTab) else None

    def _navigate_to_wol(self):
        tab = self._current_tab()
        if tab:
            tab.load(self.lang.wol_url)

    def _navigate_from_bar(self):
        text = self.url_edit.text().strip()
        if not text:
            return
        text = normalize_browser_input(text, search_if_text=True)
        tab = self._current_tab()
        if tab:
            tab.load(text)

    def dragEnterEvent(self, event):
        if self._url_from_mime_data(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._url_from_mime_data(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        url = self._url_from_mime_data(event.mimeData())
        if not url:
            super().dropEvent(event)
            return

        tab = self._current_tab()
        if tab:
            tab.load(url)
            self.url_edit.setText(url)
        event.acceptProposedAction()

    @staticmethod
    def _url_from_mime_data(mime_data) -> str:
        if mime_data.hasUrls():
            urls = mime_data.urls()
            if urls:
                url = urls[0]
                if url.isLocalFile():
                    return Path(url.toLocalFile()).resolve().as_uri()
                return normalize_browser_input(url.toString())
        if mime_data.hasText():
            return normalize_browser_input(mime_data.text(), search_if_text=True)
        return ""

    def _go_back(self):
        t = self._current_tab()
        if t and t.view.can_go_back():
            t.view.back()

    def _go_forward(self):
        t = self._current_tab()
        if t and t.view.can_go_forward():
            t.view.forward()

    def _reload(self):
        t = self._current_tab()
        if t:
            t.view.reload()

    def _clear_cookies_and_reload(self):
        tab = self._current_tab()
        if tab:
            tab.view.clear_cookies()
        QTimer.singleShot(150, self._reload)

    def _update_nav_buttons(self):
        tab = self._current_tab()
        if tab:
            self.back_btn.setEnabled(tab.view.can_go_back())
            self.fwd_btn.setEnabled(tab.view.can_go_forward())
        else:
            self.back_btn.setEnabled(False)
            self.fwd_btn.setEnabled(False)

    def _on_tab_changed(self, idx: int):
        if self.crop_btn.isChecked():
            self._cancel_crop_mode()

        self._stack.setCurrentIndex(idx)
        tab = self._current_tab()
        if tab:
            self._activate_tab(tab)
            u = tab.current_url
            self.url_edit.setText("" if u in ("about:blank", "") else u)
            if self._cursor_spotlight_active:
                tab.view.page().runJavaScript(self._CURSOR_SPOTLIGHT_JS)
        self._update_nav_buttons()

        current_widget = self._stack.widget(idx)
        is_pinned = (
            self._pinned_tab is not None
            and current_widget is not None
            and current_widget is self._pinned_tab
        )
        self.cast_btn.blockSignals(True)
        self.cast_btn.setChecked(is_pinned)
        self._update_cast_btn_visual(is_pinned)
        self.cast_btn.blockSignals(False)

    def _on_url_changed(self, tab: "BrowserTab", url: str):
        if tab is self._current_tab():
            self.url_edit.setText("" if url in ("about:blank", "") else url)
        self._update_nav_buttons()

    def _on_load_finished(self, tab: "BrowserTab"):
        if tab is self._current_tab():
            self._update_nav_buttons()
        if self._cursor_spotlight_active:
            tab.view.page().runJavaScript(self._CURSOR_SPOTLIGHT_JS)

    def _update_tab_title(self, tab: "BrowserTab", title: str):
        idx = self._stack.indexOf(tab)
        if idx >= 0:
            short = (title[:16] + "…") if len(title) > 16 else title
            label = self.tr("New tab")
            self._tab_bar.setTabText(idx, short or label)
            self._tab_bar.setTabToolTip(idx, title)

    def _on_load_progress(self, progress: int):
        self.loading_bar.setVisible(progress < 100)
