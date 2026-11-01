"""Qt lifetime/event-loop adapter for the binding's Cocoa surface presenter."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)


class CocoaSurfacePresenterController(QObject):
    def __init__(self, surface) -> None:
        super().__init__(surface)
        self._surface = surface
        self._presenter = None
        self._handle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._poll)
        surface.destroyed.connect(self.close)

    @property
    def endpoint(self) -> str:
        from pylibobs.macos_surfaces import SurfacePresenter

        handle = self._surface.native_handle
        if self._presenter is None or self._handle != handle:
            self.close()
            self._presenter = SurfacePresenter(handle)
            self._handle = handle
        self.set_visible(self._surface.isVisible())
        return self._presenter.endpoint

    def set_visible(self, visible: bool) -> None:
        if visible and self._presenter is not None:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _poll(self) -> None:
        if self._presenter is None:
            return
        try:
            self._presenter.poll()
        except Exception:  # noqa: BLE001 - native Cocoa/Mach boundary
            log.exception("Native projection surface presentation failed")
            self.close()
            self._surface.geometry_changed.emit()

    def close(self, *_args) -> None:
        self._timer.stop()
        presenter, self._presenter = self._presenter, None
        self._handle = 0
        if presenter is not None:
            presenter.close()
