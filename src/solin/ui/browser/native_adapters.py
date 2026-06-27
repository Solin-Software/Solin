from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot


__all__ = (
    "BrowserBridge",
    "HistoryAdapter",
    "NativePageAdapter",
    "UrlValue",
)


class BrowserBridge(QObject):
    project_image_signal = Signal(str)
    project_video_signal = Signal(str)
    crop_selected_signal = Signal(float, float, float, float)
    crop_cancelled_signal = Signal()
    save_media_signal = Signal(str, str)
    add_to_playlist_signal = Signal(str, str, str)

    @Slot(str)
    def projectImage(self, data: str):
        self.project_image_signal.emit(data)

    @Slot(str)
    def projectVideo(self, url: str):
        self.project_video_signal.emit(url)

    @Slot(float, float, float, float)
    def cropSelected(self, x: float, y: float, w: float, h: float):
        self.crop_selected_signal.emit(x, y, w, h)

    @Slot()
    def cancelCrop(self):
        self.crop_cancelled_signal.emit()

    @Slot(str, str)
    def saveMedia(self, url: str, media_type: str):
        self.save_media_signal.emit(url, media_type)

    @Slot(str, str, str)
    def addToPlaylist(self, url: str, title: str, media_type: str):
        self.add_to_playlist_signal.emit(url, title, media_type)


class UrlValue:
    """Small compatibility wrapper for the old QUrl.toString() call sites."""

    def __init__(self, value: str = ""):
        self._value = value or ""

    def toString(self) -> str:
        return self._value


class HistoryAdapter:
    def __init__(self, view):
        self._view = view

    def canGoBack(self) -> bool:
        return self._view.can_go_back()

    def canGoForward(self) -> bool:
        return self._view.can_go_forward()


class NativePageAdapter:
    def __init__(self, view):
        self._view = view

    def runJavaScript(self, script: str):
        self._view.run_javascript(script)

    def zoomFactor(self) -> float:
        return self._view.zoom_factor()
