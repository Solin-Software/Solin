#!/usr/bin/env python3
"""Standalone libobs playback demo — the pylibobs media pipeline in isolation.

Runs the same modules the Solin migration uses (``obs_runtime`` + the libobs
``ObsMediaController`` + a libobs ``Display``) in a tiny window, so the audio +
video path can be verified on real hardware without launching the whole app.

Usage:
    python tools/obs_media_player.py /path/to/media.mp4

Space toggles play/pause, Esc quits. Requires the ``obs`` extras (pylibobs) and
a display (X11/Wayland on Linux, native on Windows/macOS).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

# Allow running straight from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from solin.core.media.obs_runtime import obs_runtime  # noqa: E402
from solin.core.media.playback_request import MediaPlaybackRequest  # noqa: E402


class _Settings:
    """Minimal MediaPlaybackSettings stand-in for the demo."""

    def auto_download_on_play(self) -> bool:
        return False


class ObsVideoSurface(QtWidgets.QWidget):
    """Native window that hosts a libobs Display rendering the OBS canvas."""

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._display = None
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setStyleSheet("background: black;")
        self.setMinimumSize(320, 180)

    def paintEngine(self):  # noqa: D401 - libobs paints this surface, not Qt
        return None

    def attach(self) -> None:
        if self._display is not None:
            return
        ob = self._runtime.ob
        canvas = self._runtime.video
        wid = int(self.winId())
        self._display = ob.Display.from_window(wid, self.width(), self.height())

        def _draw(cx: int, cy: int) -> None:
            ob.render_main_texture_letterboxed(canvas.width, canvas.height, cx, cy)

        self._display.add_draw_callback(_draw)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._display is not None:
            self._display.resize(self.width(), self.height())

    def release(self) -> None:
        if self._display is not None:
            self._display.release()
            self._display = None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    media_path = sys.argv[1]

    app = QtWidgets.QApplication(sys.argv)

    runtime = obs_runtime()
    runtime.ensure_started(width=1280, height=720, fps=30)

    from solin.core.media.obs_playback import ObsMediaController

    controller = ObsMediaController(
        _Settings(),
        _Cache(runtime),
        downloader_factory=lambda parent: _NullDownloader(parent),
        parent=app,
    )

    win = QtWidgets.QWidget()
    win.setWindowTitle("Solin · libobs media demo")
    win.resize(960, 600)
    layout = QtWidgets.QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    surface = ObsVideoSurface(runtime)
    layout.addWidget(surface, 1)
    status = QtWidgets.QLabel("Space: play/pause   Esc: quit")
    status.setStyleSheet("color:#ccc;background:#111;padding:4px;")
    layout.addWidget(status)
    win.show()
    app.processEvents()
    surface.attach()

    controller.start_playback(MediaPlaybackRequest(source=media_path, autoplay=True))

    def update_status() -> None:
        status.setText(
            f"{'▶' if controller.is_playing else '⏸'}  "
            f"{controller.position/1000:.1f}s / {controller.duration/1000:.1f}s   "
            f"Space: play/pause   Esc: quit"
        )

    timer = QtCore.QTimer(win)
    timer.timeout.connect(update_status)
    timer.start(200)
    controller.media_ended.connect(controller.replay)  # loop

    def key(event: QtGui.QKeyEvent) -> None:
        if event.key() == QtCore.Qt.Key.Key_Escape:
            win.close()
        elif event.key() == QtCore.Qt.Key.Key_Space:
            controller.toggle_play_pause()

    win.keyPressEvent = key  # type: ignore[method-assign]

    def cleanup() -> None:
        controller.stop()
        controller.shutdown()
        surface.release()
        runtime.shutdown()

    app.aboutToQuit.connect(cleanup)

    # Headless smoke-test hook: auto-quit after N ms when set.
    import os

    quit_ms = os.environ.get("SOLIN_DEMO_QUIT_MS")
    if quit_ms:
        QtCore.QTimer.singleShot(int(quit_ms), win.close)

    return app.exec()


# ── Minimal cache/downloader doubles so the demo needs no profile state ──────


class _Cache:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    @staticmethod
    def is_remote(url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def notify_cached(self, url: str) -> None:  # pragma: no cover - demo stub
        pass


class _NullDownloader(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def get_cached_path(self, url: str):
        return None

    def start(self, url: str, *, persist: bool) -> None:
        pass

    def cancel(self) -> None:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
