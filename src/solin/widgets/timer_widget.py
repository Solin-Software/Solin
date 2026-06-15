"""
timer_widget.py — Solin
=======================
Host for the QML-based Timer tab.

The class name (``TimerWidget``) and the ``project_timer_signal`` contract are
preserved so the rest of the app (the UI controller and the timer-theme
controller that projects the media-window countdown) keep working unchanged.
Internally it now hosts ``TimerView.qml`` and wires it to :class:`TimerBridge`,
which owns the advanced-timer state.
"""

from __future__ import annotations

from PySide6.QtCore import Signal, QDateTime, QEvent, QUrl
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.i18n.manager import LanguageManager
from ..core.ui.fonts import timer_digit_font_family
from solin.ui.qml.loader import load_qml_type
from solin.ui.qml.timer_bridge import TimerBridge
from solin.ui.qml.timer_icons import TimerIconProvider


class TimerWidget(QWidget):
    """QML host for the advanced timer / media-countdown tab."""

    # Kept for backward compatibility: the media-window countdown mode emits a
    # target QDateTime that MainWindow projects via the timer-theme controller.
    project_timer_signal = Signal(QDateTime)

    def __init__(
        self,
        lang: "LanguageManager | None" = None,
        *,
        bridge: TimerBridge,
        parent=None,
    ):
        super().__init__(parent)
        self.lang = lang
        self.bridge = bridge
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._qml = QQuickWidget()
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self._qml.setFormat(fmt)
        self._qml.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self._qml.setClearColor(QColor("#0d1117"))
        self._qml.engine().addImageProvider("timericons", TimerIconProvider())
        self._qml.rootContext().setContextProperty(
            "timerDigitFontFamily",
            timer_digit_font_family(),
        )

        self.bridge.setParent(self)
        self.bridge.mediaCountdownRequested.connect(self.project_timer_signal)
        self._qml.rootContext().setContextProperty("timer", self.bridge)

        load_qml_type(self._qml, "TimerView")
        layout.addWidget(self._qml)

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

    def refresh_language(self) -> None:
        self.retranslateUi()

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
        try:
            self._qml.setSource(QUrl())
        except Exception:  # noqa: BLE001 - QML engine lifecycle boundary
            log_ignored_exception(__name__, "Could not clear timer QML source")
