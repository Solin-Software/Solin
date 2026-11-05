"""Qt lifetime/event-loop adapter for the binding's Cocoa surface presenter."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)


class CocoaSurfacePresenterController(QObject):
    _RETRY_DELAYS_MS = (250, 1000, 4000)
    _RECOVERY_FRAME_COUNT = 60

    def __init__(self, surface) -> None:
        super().__init__(surface)
        self._surface = surface
        self._presenter = None
        self._handle = 0
        self._endpoint = ""
        self._visible = surface.isVisible()
        self._failures = 0
        self._recovery_frames = 0
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._poll)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry)
        surface.destroyed.connect(self.close)

    @property
    def endpoint(self) -> str:
        handle = self._surface.native_handle
        if self._handle != handle:
            self._release_presenter()
            self._failures = 0
            self._handle = handle
        self.set_visible(self._surface.isVisible())
        if self._failures > len(self._RETRY_DELAYS_MS):
            raise RuntimeError("Cocoa surface presenter recovery exhausted")
        if self._presenter is None and not self._failures:
            self._create_presenter()
        if not self._endpoint:
            raise RuntimeError("Cocoa surface presenter recovery is unavailable")
        # Keep the last transport target until its replacement is ready. Returning
        # an empty endpoint would route callers to a foreign-process NSView;
        # raising during backoff would make them hide this surface and cancel retries.
        return self._endpoint

    def _create_presenter(self, *, notify: bool = False) -> None:
        try:
            from pylibobs.macos_surfaces import SurfacePresenter

            self._presenter = SurfacePresenter(self._handle)
        except Exception:  # noqa: BLE001 - native Cocoa/Mach construction boundary
            self._record_failure()
            raise
        previous, self._endpoint = self._endpoint, self._presenter.endpoint
        self._retry_timer.stop()
        if self._visible:
            self._timer.start()
        if notify or (previous and previous != self._endpoint):
            self._surface.geometry_changed.emit()

    def set_visible(self, visible: bool) -> None:
        was_visible, self._visible = self._visible, visible
        if not visible:
            self._timer.stop()
            self._retry_timer.stop()
            return
        if not was_visible and 0 < self._failures <= len(self._RETRY_DELAYS_MS):
            # Resume a failed session before exhaustion. Once exhausted, only
            # close/reopen or a new handle can reset it: fallback reconciliation
            # itself hides/shows the surface and must not start another retry loop.
            self._failures = 0
            self._recovery_frames = 0
            if self._presenter is None and self._handle:
                self._retry_timer.start(0)
        if self._presenter is not None:
            if not self._timer.isActive():
                self._timer.start()

    def _poll(self) -> None:
        if self._presenter is None or not self._visible:
            return
        try:
            presented = self._presenter.poll()
        except Exception:  # noqa: BLE001 - native Cocoa/Mach boundary
            log.exception("Native projection surface presentation failed")
            self._record_failure()
            return
        # Empty polls are normal between frames and cannot prove recovery. Require
        # sustained frame delivery by this presenter before allowing another budget.
        if presented and self._failures:
            self._recovery_frames += 1
            if self._recovery_frames >= self._RECOVERY_FRAME_COUNT:
                self._failures = 0
                self._recovery_frames = 0

    def _record_failure(self) -> None:
        self._release_presenter()
        self._failures += 1
        if self._visible and self._failures <= len(self._RETRY_DELAYS_MS):
            self._retry_timer.start(self._RETRY_DELAYS_MS[self._failures - 1])
        elif self._failures == len(self._RETRY_DELAYS_MS) + 1:
            # Publish the terminal state before asking targets to use their
            # existing error/fallback path. Never re-emit on endpoint reads.
            self._surface.geometry_changed.emit()

    def _retry(self) -> None:
        if (
            not self._visible
            or not self._handle
            or self._presenter is not None
            or self._failures > len(self._RETRY_DELAYS_MS)
        ):
            return
        handle = self._surface.native_handle
        if handle != self._handle:
            self._handle = handle
            self._failures = 0
        try:
            self._create_presenter(notify=True)
        except Exception:  # noqa: BLE001 - recovery must not escape the Qt timer callback
            log.exception("Could not recover native projection surface presenter")

    def _release_presenter(self) -> None:
        self._timer.stop()
        self._retry_timer.stop()
        self._recovery_frames = 0
        presenter, self._presenter = self._presenter, None
        if presenter is not None:
            try:
                presenter.close()
            except Exception:  # noqa: BLE001 - teardown must not escape Qt callbacks
                log.exception("Could not close native projection surface presenter")

    def close(self, *_args) -> None:
        self._release_presenter()
        self._handle = 0
        self._endpoint = ""
        self._failures = 0
