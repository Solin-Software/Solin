"""
timer_widget.py — Solin
=======================
Host for the QML-based Timer tab.

The widget hosts ``TimerView.qml`` and wires it to :class:`TimerBridge`, which
owns the advanced-timer state. ``project_timer_signal`` remains the presentation
contract for media-window countdown projection.
"""

from __future__ import annotations

from PySide6.QtCore import Signal, QDateTime, QEvent, QUrl
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.i18n.manager import LanguageManager
from ..ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from ..ui.fonts import timer_digit_font_family
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.timer_bridge import TimerBridge
from solin.ui.qml.timer_icons import TimerIconProvider
from solin.styles.theme import PALETTE


class TimerWidget(QWidget):
    """QML host for the advanced timer / media-countdown tab."""

    # Media-window countdown mode emits the target and selected presentation.
    project_timer_signal = Signal(QDateTime, str)
    meeting_schedule_requested = Signal()

    def __init__(
        self,
        lang: "LanguageManager | None" = None,
        *,
        bridge: TimerBridge,
        defer_qml: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.lang = lang
        self.bridge = bridge
        self._defer_qml = defer_qml
        self._qml_pointer_depth = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.bridge.setParent(self)
        self.bridge.mediaCountdownRequested.connect(self.project_timer_signal)
        self.bridge.meetingScheduleConfigurationRequested.connect(
            self.meeting_schedule_requested
        )
        self.bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.bridge.pointerExited.connect(self.end_qml_pointer_cursor)
        self._qml = QQuickWidget(self)
        self._qml.installEventFilter(self)
        self.qml_load_handle = configure_qml_host(
            self._qml,
            type_name="TimerView",
            clear_color=PALETTE.bg0,
            image_providers={"timericons": TimerIconProvider()},
            context_properties={
                "timerDigitFontFamily": timer_digit_font_family(),
                "timer": self.bridge,
            },
            mouse_tracking=True,
            defer_load=self._defer_qml,
        )
        layout.addWidget(self._qml)

    # ── QML cursor lifecycle ───────────────────────────────────────────────

    def begin_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self._qml)

    def end_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self._qml)

    def _reset_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self._qml)

    def eventFilter(self, obj, event: QEvent) -> bool:
        if obj is getattr(self, "_qml", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    # ── i18n ────────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            try:
                self._qml.engine().retranslate()
            except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
                log_ignored_exception(__name__, "Could not retranslate timer QML engine")
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        try:
            self._qml.engine().retranslate()
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not refresh timer language")
        self.bridge.refresh_language()

    def apply_theme(self) -> None:
        apply_qml_theme(self._qml, clear_color=PALETTE.bg0)
        self.bridge.refresh_theme()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Tear down the QML scene up-front during shutdown.

        On app close Qt destroys the ``TimerBridge`` (the ``timer`` context
        object) while the QML scene is still alive, so its bindings briefly
        re-evaluate against a null ``timer`` and log harmless but noisy
        ``TypeError: Cannot read property ... of null`` messages. Clearing the
        source here removes the bindings while the bridge still exists, so the
        teardown is clean. Invoked by ShutdownController.cleanup_widgets().
        """
        self._reset_qml_pointer_cursor()
        self.qml_load_handle.cancel()
        try:
            self._qml.setSource(QUrl())
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not clear timer QML source")
