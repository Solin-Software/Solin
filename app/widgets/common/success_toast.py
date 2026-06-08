"""Non-blocking success toast widget."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget

from app.styles.icons import make_icon

# ── Toast de confirmação (overlay não-bloqueante) ─────────────────────────────

class _SuccessToast(QWidget):
    """
    Toast compacto que aparece sobre a janela principal e some sozinho.
    Não é modal — não bloqueia a UI nem exige ação do usuário.
    """
    _ICON_CHECK = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round"'
        ' stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
    )

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("SuccessToast")
        self.setStyleSheet(
            "#SuccessToast{"
            "background:#161b22;"
            "border:1px solid #238636;"
            "border-radius:10px;"
            "}"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 18, 10)
        lay.setSpacing(10)

        # Ícone check
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(18, 18)
        icon_lbl.setPixmap(make_icon(self._ICON_CHECK, 18, "#3fb950").pixmap(18, 18))
        icon_lbl.setStyleSheet("background:transparent;")

        # Texto
        self._msg = QLabel()
        self._msg.setStyleSheet(
            "color:#e6edf3;font-size:12px;font-weight:500;background:transparent;"
        )

        lay.addWidget(icon_lbl)
        lay.addWidget(self._msg)

        # Animação de opacidade
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._anim.finished.connect(self._on_anim_done)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)

        self.hide()

    def show_message(self, msg: str, duration_ms: int = 2200):
        self._timer.stop()
        self._anim.stop()
        self._msg.setText(msg)
        self.adjustSize()
        self._reposition()
        self._opacity_effect.setOpacity(1.0)
        self.raise_()
        self.show()
        self._timer.start(duration_ms)

    def _reposition(self):
        p = self.parent()
        if p:
            self.move(
                (p.width() - self.width()) // 2,
                p.height() - self.height() - 80,
            )

    def resizeEvent(self, e):
        self._reposition()
        super().resizeEvent(e)

    def _fade_out(self):
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(380)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

    def _on_anim_done(self):
        if self._opacity_effect.opacity() < 0.05:
            self.hide()

