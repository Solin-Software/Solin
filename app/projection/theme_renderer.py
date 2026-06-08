"""
theme_renderer.py  —  shared text-layout engine for the sermon-theme slide.

Public API
----------
render_theme_slide(painter, w, h, renderer, theme_text, subtitle)

Algorithm
---------
1. The SVG background is drawn first, scaled to fill (w, h).
2. The text area starts at LEFT_FRAC * w and is TEXT_W_FRAC * w wide.
3. Title top anchor: TOP_FRAC * h.
4. Maximum title height budget: MAX_TITLE_H_FRAC * h.
5. Binary-search the largest integer pixel size so that the wrapped
   title fits inside (text_w × max_title_h).
6. Measure the exact bounding rect of the laid-out lines.
7. Draw the subtitle at  title_top + real_title_height + GAP_FRAC * h.
   If that would push the subtitle below SUBTITLE_MAX_BOTTOM * h,
   the subtitle is clamped to that limit (it still never overlaps the title
   because we already guaranteed the title fits).
"""

from __future__ import annotations
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtSvg import QSvgRenderer

# ── Layout constants (fractions of slide dimensions) ─────────────────────────
LEFT_FRAC           = 0.155   # title / subtitle left edge
TEXT_W_FRAC         = 0.720   # text column width
TOP_FRAC            = 0.12    # title top anchor
MAX_TITLE_H_FRAC    = 0.60    # maximum height budget for title block
GAP_FRAC            = 0.045   # gap between title bottom and subtitle
SUBTITLE_MAX_BOTTOM = 0.93    # subtitle must end above this

# ── Font size constraints ─────────────────────────────────────────────────────
TITLE_MAX_PX_FRAC   = 0.13    # largest title font = 13 % of h
TITLE_MIN_PX        = 18      # never go below this (px)
SUB_SIZE_RATIO      = 0.34    # subtitle = 34 % of chosen title size
SUB_MIN_PX          = 10

# ── Colours ───────────────────────────────────────────────────────────────────
TITLE_COLOR    = QColor("#3A4E42")
SUBTITLE_COLOR = QColor("#5a7a62")


def _make_title_font(px: int) -> QFont:
    f = QFont("Arial")
    f.setBold(True)
    f.setPixelSize(px)
    return f


def _make_sub_font(px: int) -> QFont:
    f = QFont("Arial")
    f.setPixelSize(max(SUB_MIN_PX, px))
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(1.0, px * 0.08))
    return f


def _wrap_lines(text: str, fm: QFontMetricsF, max_w: float) -> list[str]:
    """Word-wrap *text* into lines that fit within *max_w* pixels."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = current + " " + word
            if fm.horizontalAdvance(candidate) <= max_w:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _title_block_height(lines: list[str], line_h: float) -> float:
    """Total pixel height of the title block (all lines)."""
    return len(lines) * line_h


def _fit_font(text: str, max_w: float, max_h: float,
              h_slide: float) -> tuple[QFont, list[str], float, float]:
    """
    Binary-search the largest font size so that the wrapped title fits in
    (max_w × max_h).

    Returns (font, lines, line_height, block_height).
    """
    hi = max(TITLE_MIN_PX + 1, int(h_slide * TITLE_MAX_PX_FRAC))
    lo = TITLE_MIN_PX

    best_font  = _make_title_font(lo)
    best_lines = _wrap_lines(text, QFontMetricsF(best_font), max_w)
    best_lh    = QFontMetricsF(best_font).height()
    best_bh    = _title_block_height(best_lines, best_lh)

    # Quick path: max size already fits
    f_hi = _make_title_font(hi)
    fm_hi = QFontMetricsF(f_hi)
    lines_hi = _wrap_lines(text, fm_hi, max_w)
    lh_hi = fm_hi.height()
    bh_hi = _title_block_height(lines_hi, lh_hi)
    if bh_hi <= max_h:
        return f_hi, lines_hi, lh_hi, bh_hi

    # Binary search
    while lo < hi - 1:
        mid = (lo + hi) // 2
        f = _make_title_font(mid)
        fm = QFontMetricsF(f)
        lines = _wrap_lines(text, fm, max_w)
        lh = fm.height()
        bh = _title_block_height(lines, lh)
        if bh <= max_h:
            lo = mid
            best_font  = f
            best_lines = lines
            best_lh    = lh
            best_bh    = bh
        else:
            hi = mid

    return best_font, best_lines, best_lh, best_bh


def render_theme_slide(
    painter: QPainter,
    w: int,
    h: int,
    renderer: QSvgRenderer,
    theme_text: str,
    subtitle: str,
) -> None:
    """
    Full slide render: SVG background + responsive title + dynamic subtitle.
    Call inside paintEvent after creating the QPainter.
    """
    p = painter
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # ── Background ────────────────────────────────────────────────────────
    renderer.render(p, QRectF(0, 0, w, h))

    if not theme_text:
        return

    text_x  = w * LEFT_FRAC
    text_w  = w * TEXT_W_FRAC
    title_top = h * TOP_FRAC
    max_title_h = h * MAX_TITLE_H_FRAC

    # ── Fit title font ────────────────────────────────────────────────────
    font, lines, line_h, block_h = _fit_font(
        theme_text, text_w, max_title_h, h
    )

    # ── Draw title lines (top-aligned within the fitted block) ────────────
    p.setFont(font)
    p.setPen(TITLE_COLOR)
    fm = QFontMetricsF(font)
    ascent = fm.ascent()

    for i, line in enumerate(lines):
        y = title_top + i * line_h + ascent
        p.drawText(QRectF(text_x, y - ascent, text_w, line_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   line)

    # ── Subtitle: gap below last title line ───────────────────────────────
    title_bottom = title_top + block_h
    gap          = h * GAP_FRAC
    sub_top      = title_bottom + gap

    sub_px   = max(SUB_MIN_PX, int(font.pixelSize() * SUB_SIZE_RATIO))
    sub_font = _make_sub_font(sub_px)
    sub_fm   = QFontMetricsF(sub_font)
    sub_h    = sub_fm.height()

    # Clamp so subtitle never goes below SUBTITLE_MAX_BOTTOM
    max_sub_top = h * SUBTITLE_MAX_BOTTOM - sub_h
    sub_top = min(sub_top, max_sub_top)

    p.setFont(sub_font)
    p.setPen(SUBTITLE_COLOR)
    p.drawText(
        QRectF(text_x, sub_top, text_w, sub_h * 1.2),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        subtitle,
    )
