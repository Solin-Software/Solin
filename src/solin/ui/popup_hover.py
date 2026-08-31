"""Opt-in, delayed opening for popups hosted by a toolbar or another surface."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

from solin.core.foundation.settings_store import ProfileAppSettingsStore


@dataclass(frozen=True, slots=True)
class _PopupTarget:
    open_popup: Callable[[], None]
    available: Callable[[], bool]


class PopupHoverController(QObject):
    """Share one cancellable timer; retain native popup focus and dismissal.

    Registration opts a popup in without knowing its contents. The surface is
    resolved at entry, allowing hosts with more than one native rendering surface.
    """

    opening = Signal()
    OPEN_DELAY_MS = 200

    def __init__(
        self,
        owner: QWidget,
        settings: ProfileAppSettingsStore,
        surface: Callable[[], QWidget],
    ) -> None:
        super().__init__(owner)
        self._owner = owner
        self._settings = settings
        self._surface = surface
        self._enabled_ids = set(settings.hover_popup_ids())
        self._targets: dict[str, _PopupTarget] = {}
        self._pending: tuple[str, QWidget, QRect] | None = None
        self._blocked: QRect | None = None
        self._filter_installed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.OPEN_DELAY_MS)
        self._timer.timeout.connect(self._open_pending)
        owner.destroyed.connect(self._dispose)

    def _dispose(self) -> None:
        # A host may destroy other native surfaces from its destroyed signal,
        # before this child is deleted. Stop filtering without touching the host.
        self._pending = None
        self._blocked = None
        self._sync_event_filter()
        self._timer.stop()

    def register(
        self,
        popup_id: str,
        open_popup: Callable[[], None],
        available: Callable[[], bool],
    ) -> None:
        if not popup_id or popup_id in self._targets:
            raise ValueError(f"Invalid or duplicate hover popup: {popup_id!r}")
        self._targets[popup_id] = _PopupTarget(open_popup, available)

    def bind_button(self, popup_id: str, button: QAbstractButton) -> None:
        if popup_id not in self._targets:
            raise KeyError(popup_id)
        button.setChecked(popup_id in self._enabled_ids)
        button.toggled.connect(lambda enabled: self.set_enabled(popup_id, enabled))
        button.show()

    def set_enabled(self, popup_id: str, enabled: bool) -> None:
        if popup_id not in self._targets:
            raise KeyError(popup_id)
        if enabled == (popup_id in self._enabled_ids):
            return
        self._settings.set_popup_hover_enabled(popup_id, enabled)
        if enabled:
            self._enabled_ids.add(popup_id)
        else:
            self._enabled_ids.discard(popup_id)
            if self._pending is not None and self._pending[0] == popup_id:
                self.cancel()

    def enter(self, popup_id: str, x: float, y: float, width: float, height: float) -> None:
        self.cancel()
        if popup_id not in self._enabled_ids or popup_id not in self._targets:
            return
        surface = self._surface()
        rect = QRect(round(x), round(y), round(width), round(height))
        if not rect.isValid() or not self._can_open(popup_id, surface, rect):
            return
        if self._blocked is not None:
            return
        self._pending = (popup_id, surface, rect)
        self._sync_event_filter()
        self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()
        self._pending = None
        self._release_block_outside_anchor()
        self._sync_event_filter()

    def _can_open(self, popup_id: str, surface: QWidget, rect: QRect) -> bool:
        return (
            surface is self._surface()
            and surface.isVisible()
            and surface.isEnabled()
            and (self._owner.window().isActiveWindow() or surface.window().isActiveWindow())
            and QApplication.activePopupWidget() is None
            and QApplication.activeModalWidget() is None
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
            and rect.contains(surface.mapFromGlobal(QCursor.pos()))
            and self._targets[popup_id].available()
        )

    def _open_pending(self) -> None:
        pending = self._pending
        self.cancel()
        if pending is None:
            return
        popup_id, surface, rect = pending
        if popup_id not in self._enabled_ids or not self._can_open(popup_id, surface, rect):
            return
        # Native popup dismissal can synthesize Enter without pointer movement.
        # Require a real exit before opening again at this anchor.
        self._blocked = QRect(surface.mapToGlobal(rect.topLeft()), rect.size())
        self._sync_event_filter()
        self.opening.emit()
        self._targets[popup_id].open_popup()

    def _release_block_outside_anchor(self) -> None:
        if self._blocked is not None and not self._blocked.contains(QCursor.pos()):
            self._blocked = None

    def _sync_event_filter(self) -> None:
        app = QApplication.instance()
        needed = self._pending is not None or self._blocked is not None
        if app is not None and needed != self._filter_installed:
            if needed:
                app.installEventFilter(self)
            else:
                app.removeEventFilter(self)
            self._filter_installed = needed

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Type.MouseMove, QEvent.Type.HoverMove, QEvent.Type.Leave):
            self._release_block_outside_anchor()
            self._sync_event_filter()
        elif event_type in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.KeyPress,
            QEvent.Type.WindowDeactivate,
            QEvent.Type.ApplicationDeactivate,
        ):
            self.cancel()
        elif self._pending is not None and event_type in (
            QEvent.Type.Hide,
            QEvent.Type.Move,
            QEvent.Type.Resize,
        ):
            surface = self._pending[1]
            if isinstance(watched, QWidget) and (
                watched is surface or watched.isAncestorOf(surface)
            ):
                self.cancel()
        return False
