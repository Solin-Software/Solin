"""Reusable annual-text projection surface with an optional countdown overlay."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QPainter,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from ..core.rendering.fonts import FontManager
from ..core.timer.render import format_fixed_countdown
from ..ui.fonts import timer_digit_font_family

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

        painter.fillRect(badge_x, badge_y, badge_size, badge_size, QColor(51, 51, 51))

        jw_svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 151 153">
  <path fill="black" d="M47.34 87c.22-13.47-.15-26.95.16-40.43 2.33 0 4.66 0 6.99-.01.25 13.48.1 26.97.08 40.46.09 6.42-3.16 13.91-9.88 15.63-6.87 1.58-14.59.65-20.24-3.8 1.03-1.94 2.09-3.86 3.14-5.78 3.76 2.25 7.87 4.83 12.46 4.07 4.68-.73 7.34-5.74 7.29-10.14m14.14-40.42c2.57.01 5.15.01 7.72.05 1.74 9.51 4.19 18.88 5.95 28.39.93 4.65 1.7 9.36 3.24 13.86 4.04-13.41 8.11-26.81 12.45-40.12 1.83-.06 3.66-.11 5.5-.15 3.19 7.38 4.6 15.36 7.14 22.97 1.89 5.54 3.1 11.28 4.95 16.84 1.12-1.86 1.63-3.97 2.02-6.09 2.35-11.94 5.51-23.7 7.76-35.66 2.51-.08 5.02-.08 7.53-.11-4.7 18.69-9.12 37.44-13.86 56.11-2.11.01-4.22 0-6.33 0-4.19-13.92-7.88-27.99-11.96-41.95-4.46 13.96-8.67 28.01-13.2 41.94-1.99.02-3.97.04-5.95.05-4.6-18.64-8.58-37.43-12.96-56.13"/>
</svg>"""
        renderer = QSvgRenderer(QByteArray(jw_svg))
        jw_pixmap = QPixmap(badge_size, badge_size)
        jw_pixmap.fill(QColor(51, 51, 51))
        pix_painter = QPainter(jw_pixmap)
        pix_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad = int(badge_size * -0.08)
        renderer.render(pix_painter, QRectF(pad, pad, badge_size - 2 * pad, badge_size - 2 * pad))
        pix_painter.end()
        painter.drawPixmap(badge_x, badge_y, jw_pixmap)

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
        font_size = max(18, int(min(width, height) * 0.075))
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
