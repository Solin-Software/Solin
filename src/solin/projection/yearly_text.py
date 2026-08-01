"""Reusable annual-text projection surface with an optional countdown overlay."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QPainter,
)
from PySide6.QtWidgets import QWidget

from ..core.rendering.fonts import FontManager
from ..core.timer.render import format_fixed_countdown
from ..ui.fonts import timer_digit_font_family
from .brand import render_jw_badge

__all__ = ("YearlyTextWidget",)

_WT_CLEAR_TEXT = "Wt-ClearText-Bold"


@dataclass(frozen=True, slots=True)
class _CountdownLayout:
    font: QFont
    text: str
    text_rect: QRectF
    bar_rect: QRectF


class YearlyTextWidget(QWidget):
    """Render annual text and its optional media-countdown presentation.

    The component owns only visual state and responsive layout. Timer lifecycle,
    transitions, and media routing remain responsibilities of its consumers.
    """

    def __init__(
        self,
        font_manager: FontManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._font_manager = font_manager
        self.setStyleSheet("background-color: black;")
        self._quote = ""
        self._reference = ""
        self._api_code = ""
        self._countdown_remaining: int | None = None
        self._countdown_total = 1
        self._countdown_blink = False

        # Kick off the font download/registration in background.
        self._font_manager.font_ready.connect(self._on_font_ready)
        self._font_manager.ensure(_WT_CLEAR_TEXT)

    def _on_font_ready(self, font_name: str) -> None:
        """Triggered once Wt-ClearText-Bold is registered; repaint if visible."""
        if font_name == _WT_CLEAR_TEXT:
            self.update()

    def set_text(self, quote: str, reference: str, api_code: str = "") -> None:
        self._quote = quote
        self._reference = reference
        self._api_code = api_code
        self.update()

    def set_countdown(self, remaining: int, total: int) -> None:
        self._countdown_remaining = max(0, int(remaining))
        self._countdown_total = max(1, int(total))
        self.update()

    def clear_countdown(self) -> None:
        if self._countdown_remaining is None:
            return
        self._countdown_remaining = None
        self._countdown_blink = False
        self.update()

    def set_countdown_blink(self, on: bool) -> None:
        self._countdown_blink = bool(on)
        if self._countdown_remaining is not None:
            self.update()

    def paintEvent(self, _event) -> None:
        countdown_layout = self._countdown_layout()
        if not self._quote and not self._reference:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(0, 0, 0))
            self._paint_countdown(painter, countdown_layout)
            painter.end()
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w = self.width()
        h = self.height()

        # Black background
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        # ── Typography & Auto-Scaling ─────────────────────────────────────
        font = QFont()
        font.setFamilies(
            [self._font_manager.family(_WT_CLEAR_TEXT), "Georgia", "Noto Serif"]
        )
        font.setWeight(QFont.Weight.Normal)

        if self._api_code == "J":
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 115)
        else:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)

        # Extrai as linhas originais
        quote_lines = [line for line in self._quote.split("\n") if line.strip()]
        ref_text = self._reference if self._reference else ""
        all_lines = quote_lines + ([ref_text] if ref_text else [])

        # Proporções
        max_base_size = max(12, int(w * 0.040))
        max_text_w = int(w * 0.70)
        max_text_h = int(h * 0.80)
        total_lines = len(quote_lines) + (1 if ref_text else 0)

        def measure_text_block(size: int) -> tuple[int, int]:
            font.setPixelSize(size)
            metrics = QFontMetrics(font)
            max_line_width = max(
                (metrics.horizontalAdvance(line) for line in all_lines),
                default=0,
            )
            line_height = int(size * 1.45)
            ref_spacing = int(size * 0.45)
            return max_line_width, total_lines * line_height + ref_spacing

        # Preserve the original annual-text layout. The countdown only imposes
        # an extra bottom boundary when the unchanged text would collide with it.
        best_size = 8
        for size in range(max_base_size, 7, -1):
            max_line_width, total_h = measure_text_block(size)
            if max_line_width <= max_text_w and total_h <= max_text_h:
                best_size = size
                break

        if countdown_layout is not None:
            countdown_top = countdown_layout.text_rect.top()
            bottom_limit = countdown_top - max(8, int(h * 0.025))
            collision_safe_size = 1
            for size in range(best_size, 0, -1):
                max_line_width, total_h = measure_text_block(size)
                start_y = (h - total_h) // 2
                if (
                    max_line_width <= max_text_w
                    and total_h <= max_text_h
                    and start_y + total_h <= bottom_limit
                ):
                    collision_safe_size = size
                    break
            best_size = collision_safe_size

        # ── Aplicação do Tamanho Calculado (Matemática Original) ──────────
        font.setPixelSize(best_size)
        fm = QFontMetrics(font)

        line_height = int(best_size * 1.45)
        ref_spacing = int(best_size * 0.45)
        block_h = total_lines * line_height + ref_spacing

        start_y = (h - block_h) // 2

        # Desenhar o texto (Branco)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(font)

        for i, line in enumerate(quote_lines):
            lw = fm.horizontalAdvance(line)
            lx = (w - lw) // 2
            ly = start_y + i * line_height + best_size
            painter.drawText(lx, ly, line)

        # Desenhar a referência (Agora alinhado na próxima linha normal, sem pulo extra)
        if ref_text:
            ref_y = start_y + len(quote_lines) * line_height + best_size
            rw = fm.horizontalAdvance(ref_text)
            rx = (w - rw) // 2
            painter.drawText(rx, ref_y, ref_text)

        # ── JW Badge (bottom-right) ───────────────────────────────────────
        badge_x, badge_y, badge_size = self._jw_badge_geometry()
        painter.drawPixmap(badge_x, badge_y, render_jw_badge(badge_size))

        self._paint_countdown(painter, countdown_layout)
        painter.end()

    def _jw_badge_geometry(self) -> tuple[int, int, int]:
        width = self.width()
        height = self.height()
        badge_size = int(min(width, height) * 0.15)
        badge_x = width - badge_size - int(width * 0.08)
        badge_y = height - badge_size - int(height * 0.12)
        return badge_x, badge_y, badge_size

    def _countdown_layout(self) -> _CountdownLayout | None:
        if self._countdown_remaining is None:
            return None

        width = self.width()
        height = self.height()
        font_size = max(18, int(min(width, height) * 0.099))
        font = QFont()
        font.setFamilies([timer_digit_font_family(), "Consolas", "monospace"])
        font.setPixelSize(font_size)
        font.setWeight(QFont.Weight.DemiBold)
        font.setKerning(False)

        text = format_fixed_countdown(
            self._countdown_remaining,
            self._countdown_total,
        )
        metrics = QFontMetricsF(font)
        text_width = max(1.0, metrics.horizontalAdvance(text))
        text_height = metrics.height()
        bar_height = max(2, int(font_size * 0.06))
        gap = max(4, int(font_size * 0.16))
        _, badge_y, badge_size = self._jw_badge_geometry()
        badge_center_y = badge_y + (badge_size / 2.0)
        text_rect = QRectF(
            (width - text_width) / 2.0,
            badge_center_y - (text_height / 2.0),
            text_width,
            text_height,
        )
        bar_y = text_rect.bottom() + gap
        bar_rect = QRectF((width - text_width) / 2.0, bar_y, text_width, bar_height)
        return _CountdownLayout(font, text, text_rect, bar_rect)

    def _paint_countdown(
        self,
        painter: QPainter,
        layout: _CountdownLayout | None,
    ) -> None:
        if layout is None or self._countdown_remaining is None:
            return

        active_color = QColor("#ef4444") if self._countdown_blink else QColor("#f8fafc")
        painter.save()
        painter.setFont(layout.font)
        painter.setPen(active_color)
        painter.drawText(layout.text_rect, Qt.AlignmentFlag.AlignCenter, layout.text)

        painter.fillRect(layout.bar_rect, QColor("#3f3f46"))
        progress = min(1.0, max(0.0, self._countdown_remaining / self._countdown_total))
        painter.fillRect(
            QRectF(
                layout.bar_rect.left(),
                layout.bar_rect.top(),
                layout.bar_rect.width() * progress,
                layout.bar_rect.height(),
            ),
            active_color,
        )
        painter.restore()
