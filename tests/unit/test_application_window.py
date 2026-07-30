from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QByteArray, Signal
from PySide6.QtGui import QCloseEvent, QSurface
from PySide6.QtQuick import QSGRendererInterface
from PySide6.QtWidgets import QApplication, QWidget

from solin.bootstrap.application_window import (
    _GRAPHICS_API_SURFACE_TYPES,
    _quick_surface_type,
    ApplicationWindow,
    ApplicationWindowState,
)


_APP = QApplication.instance() or QApplication([])


def _ignore_geometry(_geometry: QByteArray) -> None:
    pass


class _CancellableLoad:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Runtime(QWidget):
    first_frame_presented = Signal()
    switch_profile_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.shutdown_called = False
        self.handoff_completed = False
        self.opened_files: list[str] = []
        self.events: list[object] = []
        self.construction_aborted = False
        self.construction_committed = False

    def abort_construction(self) -> None:
        self.construction_aborted = True

    def commit_construction(self) -> None:
        self.construction_committed = True

    def complete_startup_handoff(self) -> None:
        self.handoff_completed = True
        self.events.append("handoff")

    def open_media_files(self, paths: list[str]) -> None:
        self.opened_files.extend(paths)
        self.events.append(("open", list(paths)))

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_application_window_keeps_one_native_window_through_hydration() -> None:
    script = """
import json
import time
from PySide6.QtCore import QByteArray, Signal
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication, QWidget
from solin.bootstrap.application_window import ApplicationWindow

class Runtime(QWidget):
    first_frame_presented = Signal()
    switch_profile_requested = Signal()
    def __init__(self, parent):
        super().__init__(parent)
        self.quick_surface = QQuickWidget(self)
        self.quick_surface.setFixedSize(1, 1)
        self._painted = False
    def abort_construction(self): pass
    def commit_construction(self): pass
    def complete_startup_handoff(self): pass
    def open_media_files(self, paths): pass
    def shutdown(self): pass
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._painted:
            self._painted = True
            self.first_frame_presented.emit()

app = QApplication([])
window = ApplicationWindow(
    width=900,
    height=700,
    geometry=QByteArray(),
    save_geometry=lambda _geometry: None,
    pending_files=[],
)
loading_frames = []
application_frames = []
window.loading_frame_presented.connect(lambda: loading_frames.append(True))
window.application_frame_presented.connect(lambda: application_frames.append(True))
window.show()
deadline = time.monotonic() + 1.0
while not loading_frames and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.005)
native_id = int(window.winId())
window.begin_hydration()
runtime = Runtime(window.content_parent())
window.register_runtime_candidate(runtime)
runtime_paint_state = []
runtime.first_frame_presented.connect(lambda: runtime_paint_state.append({
    "window_visible": window.isVisible(),
    "loading_visible": window._loading_canvas.isVisible(),
    "same_native_id": int(window.winId()) == native_id,
}))
window.install_runtime(runtime)
deadline = time.monotonic() + 1.0
while not application_frames and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.005)
visible_top_levels = [widget for widget in app.topLevelWidgets() if widget.isVisible()]
print(json.dumps({
    "loading": loading_frames,
    "application": application_frames,
    "runtime_paint_state": runtime_paint_state,
    "same_native_id": int(window.winId()) == native_id,
    "window_visible": window.isVisible(),
    "top_levels": len(visible_top_levels),
    "size": [window.width(), window.height()],
}))
window.close()
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )

    assert json.loads(result.stdout) == {
        "loading": [True],
        "application": [True],
        "runtime_paint_state": [
            {
                "window_visible": True,
                "loading_visible": True,
                "same_native_id": True,
            }
        ],
        "same_native_id": True,
        "window_visible": True,
        "top_levels": 1,
        "size": [900, 700],
    }


def test_application_window_starts_with_loading_canvas() -> None:
    window = ApplicationWindow(
        width=900,
        height=700,
        geometry=QByteArray(),
        save_geometry=_ignore_geometry,
        pending_files=[],
    )

    assert window._content_stack.currentWidget() is window._loading_canvas
    assert window.internalWinId() == 0
    surface_type = _quick_surface_type()
    if surface_type != QSurface.SurfaceType.RasterSurface:
        assert window.windowHandle() is not None
        assert window.windowHandle().surfaceType() == surface_type


def test_application_window_restores_maximized_state_and_normal_geometry() -> None:
    saved_geometry = []
    source = ApplicationWindow(
        width=700,
        height=650,
        geometry=QByteArray(),
        save_geometry=saved_geometry.append,
        pending_files=[],
    )
    source.show()
    _APP.processEvents()
    expected_normal_size = source.normalGeometry().size()
    source.showMaximized()
    _APP.processEvents()
    source.close()

    assert len(saved_geometry) == 1

    restored = ApplicationWindow(
        width=900,
        height=700,
        geometry=saved_geometry[0],
        save_geometry=_ignore_geometry,
        pending_files=[],
    )
    restored.show()
    _APP.processEvents()

    assert restored.isMaximized()

    restored.showNormal()
    _APP.processEvents()

    assert restored.size() == expected_normal_size
    restored.close()


def test_quick_graphics_apis_map_to_compatible_native_surfaces() -> None:
    assert _GRAPHICS_API_SURFACE_TYPES == {
        QSGRendererInterface.GraphicsApi.Software: QSurface.SurfaceType.RasterSurface,
        QSGRendererInterface.GraphicsApi.OpenVG: QSurface.SurfaceType.OpenVGSurface,
        QSGRendererInterface.GraphicsApi.OpenGL: QSurface.SurfaceType.OpenGLSurface,
        QSGRendererInterface.GraphicsApi.Direct3D11: QSurface.SurfaceType.Direct3DSurface,
        QSGRendererInterface.GraphicsApi.Vulkan: QSurface.SurfaceType.VulkanSurface,
        QSGRendererInterface.GraphicsApi.Metal: QSurface.SurfaceType.MetalSurface,
        QSGRendererInterface.GraphicsApi.Null: QSurface.SurfaceType.RasterSurface,
        QSGRendererInterface.GraphicsApi.Direct3D12: QSurface.SurfaceType.Direct3DSurface,
    }


def test_quick_surface_type_honors_software_adaptation(monkeypatch) -> None:
    monkeypatch.delenv("QSG_RHI_BACKEND", raising=False)
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")

    assert _quick_surface_type() == QSurface.SurfaceType.RasterSurface


def test_application_window_queues_files_until_runtime_is_ready() -> None:
    pending = ["before.mp4"]
    window = ApplicationWindow(
        width=900,
        height=700,
        geometry=QByteArray(),
        save_geometry=_ignore_geometry,
        pending_files=pending,
    )
    window.open_media_files(["during.mp4", "before.mp4"])
    assert pending == ["before.mp4", "during.mp4"]

    assert window.begin_hydration() is True
    runtime = _Runtime(window.content_parent())
    window.register_runtime_candidate(runtime)
    window.install_runtime(runtime)
    runtime.first_frame_presented.emit()
    assert window.state is ApplicationWindowState.HYDRATING
    window.open_media_files(["between.mp4"])
    window.complete_startup_handoff()

    assert window.state is ApplicationWindowState.READY
    assert pending == []
    assert runtime.construction_committed is True
    assert runtime.construction_aborted is False
    assert runtime.opened_files == ["before.mp4", "during.mp4", "between.mp4"]
    assert runtime.events == [
        "handoff",
        ("open", ["before.mp4", "during.mp4", "between.mp4"]),
    ]


def test_closing_application_window_cancels_load_and_shuts_down_runtime() -> None:
    window = ApplicationWindow(
        width=900,
        height=700,
        geometry=QByteArray(),
        save_geometry=_ignore_geometry,
        pending_files=[],
    )
    load = _CancellableLoad()
    window.set_load_handle(load)
    assert window.begin_hydration() is True
    runtime = _Runtime(window.content_parent())
    window.register_runtime_candidate(runtime)
    window.install_runtime(runtime)

    window.closeEvent(QCloseEvent())

    assert window.state is ApplicationWindowState.CLOSING
    assert load.cancelled is True
    assert runtime.shutdown_called is True


def test_application_close_can_be_cancelled_before_shutdown() -> None:
    window = ApplicationWindow(width=900, height=700, pending_files=[])
    assert window.begin_hydration() is True
    runtime = _Runtime(window.content_parent())
    runtime.confirm_close = lambda: False
    window.register_runtime_candidate(runtime)
    window.install_runtime(runtime)
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window.state is ApplicationWindowState.HYDRATING
    assert runtime.shutdown_called is False


def test_closing_during_runtime_construction_aborts_partial_resources() -> None:
    window = ApplicationWindow(
        width=900,
        height=700,
        geometry=QByteArray(),
        save_geometry=_ignore_geometry,
        pending_files=[],
    )
    assert window.begin_hydration() is True
    runtime = _Runtime(window.content_parent())
    window.register_runtime_candidate(runtime)

    window.closeEvent(QCloseEvent())

    assert window.state is ApplicationWindowState.CLOSING
    assert runtime.construction_aborted is True
    assert runtime.construction_committed is False
    assert runtime.shutdown_called is False
