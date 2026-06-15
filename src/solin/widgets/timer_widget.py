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

from PySide6.QtCore import QByteArray, QRectF, Qt, Signal, QDateTime, QEvent, QUrl
from PySide6.QtGui import QColor, QPainter, QPixmap, QSurfaceFormat
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.i18n.manager import LanguageManager
from ..core.ui.fonts import timer_digit_font_family
from solin.ui.qml.loader import load_qml_type
from ..styles.icons import (
    ICON_CALENDAR,
    ICON_CHEVRON_LEFT,
    ICON_CHEVRON_RIGHT,
    ICON_HOME,
    ICON_MONITOR,
    ICON_NAV_TIMER,
    ICON_PLAY,
    ICON_REC_STOP,
    ICON_REPEAT,
    ICON_SEC_LIVING,
    ICON_SEC_MINISTRY,
    ICON_SEC_PUBLIC_TALK,
    ICON_SEC_TREASURES,
    ICON_SEC_WATCHTOWER,
)
from .timer_bridge import TimerBridge


_ICON_PDF = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
     fill="none" stroke="currentColor" stroke-width="1.8"
     stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 2h8l4 4v16H6z" />
  <path d="M14 2v5h5" />
  <path d="M8 13h8" />
  <path d="M8 17h6" />
</svg>
"""


# Names the timer QML can request via ``image://timericons/<name>/<size>/<hex>``.
_TIMER_ICON_MAP = {
    "treasures": ICON_SEC_TREASURES,
    "ministry": ICON_SEC_MINISTRY,
    "living": ICON_SEC_LIVING,
    "public_talk": ICON_SEC_PUBLIC_TALK,
    "watchtower": ICON_SEC_WATCHTOWER,
    "clock": ICON_NAV_TIMER,
    "calendar": ICON_CALENDAR,
    "monitor": ICON_MONITOR,
    "play": ICON_PLAY,
    "stop": ICON_REC_STOP,
    "reset": ICON_REPEAT,
    "pdf": _ICON_PDF,
    "home": ICON_HOME,
    "chevron_left": ICON_CHEVRON_LEFT,
    "chevron_right": ICON_CHEVRON_RIGHT,
}


class TimerIconProvider(QQuickImageProvider):
    """Serves the timer tab's SVG icons, tinted and aspect-fit.

    URL: ``image://timericons/<name>/<size>/<colorHex>`` (hex without ``#``).
    Unlike a plain square render, the SVG is centred and scaled to preserve its
    aspect ratio — important for the wheat/sheep artwork whose viewBox is tall.
    """

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_str: str, size, requestedSize):  # noqa: N802
        parts = id_str.split("/")
        name = parts[0] if parts else ""
        px = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 16
        color = f"#{parts[2]}" if len(parts) > 2 else "#c9d1d9"

        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)

        svg_str = _TIMER_ICON_MAP.get(name)
        if not svg_str:
            return pix

        renderer = QSvgRenderer(QByteArray(svg_str.replace("currentColor", color).encode()))
        if not renderer.isValid():
            return pix

        # Aspect-fit the SVG viewBox into the square target.
        vb = renderer.defaultSize()
        vw = vb.width() or px
        vh = vb.height() or px
        scale = min(px / vw, px / vh)
        w = vw * scale
        h = vh * scale
        target = QRectF((px - w) / 2.0, (px - h) / 2.0, w, h)

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, target)
        painter.end()
        return pix


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
