from __future__ import annotations

from PySide6.QtGui import QPixmap

from solin.styles.theme import PALETTE


class ProjectionAudioMixin:
    def _refresh_audio_cover_in_overlay(self):
        if self._audio_cover_pixmap and not self._audio_cover_pixmap.isNull():
            self._display_cover_pixmap(self._audio_cover_pixmap)
        else:
            self._show_audio_cover_fallback()

    def _display_cover_pixmap(self, pixmap: QPixmap):
        self._stop_wave_animation()
        self.preview_content.setPixmap(pixmap)

    def _show_audio_cover_fallback(self):
        import random

        base = [22, 50, 76, 96, 76, 50, 22]
        wave_min = [10, 22, 36, 48, 36, 22, 10]
        wave_max = [38, 72, 104, 128, 104, 72, 38]
        self._wave_cur = [float(h) for h in base]
        self._wave_target = [
            float(random.randint(wave_min[i], wave_max[i])) for i in range(7)
        ]
        self._wave_speed = [0.07, 0.09, 0.11, 0.08, 0.10, 0.07, 0.09]
        self._wave_min = wave_min
        self._wave_max = wave_max
        self._wave_timer.start()
        self._render_wave_frame()

    def _on_wave_tick(self):
        import random

        changed = False
        for i in range(7):
            diff = self._wave_target[i] - self._wave_cur[i]
            self._wave_cur[i] += diff * self._wave_speed[i]
            if abs(diff) < 2.0:
                self._wave_target[i] = float(
                    random.randint(self._wave_min[i], self._wave_max[i])
                )
            changed = True
        if changed:
            self._render_wave_frame()

    def _render_wave_frame(self):
        w = self.preview_content.width()
        h = self.preview_content.height()
        if w <= 0 or h <= 0:
            return

        opacity = [0.45, 0.60, 0.78, 1.00, 0.78, 0.60, 0.45]
        bar_w = 10
        bar_gap = 8
        n_bars = 7
        total_w = n_bars * bar_w + (n_bars - 1) * bar_gap
        start_x = (200 - total_w) // 2

        bars_svg = []
        for i in range(n_bars):
            bh = max(8.0, self._wave_cur[i])
            bx = start_x + i * (bar_w + bar_gap)
            by = (200 - bh) / 2
            bars_svg.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w}" '
                f'height="{bh:.1f}" rx="5" fill="url(#bar)" opacity="{opacity[i]}"/>'
            )

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
            "<defs>"
            '<radialGradient id="glow" cx="50%" cy="50%" r="50%">'
            f'<stop offset="0%"  stop-color="{PALETTE.accent_tint}" stop-opacity="1"/>'
            f'<stop offset="100%" stop-color="{PALETTE.bg0}" stop-opacity="1"/>'
            "</radialGradient>"
            '<linearGradient id="bar" x1="0" y1="1" x2="0" y2="0">'
            f'<stop offset="0%"  stop-color="{PALETTE.accent}" stop-opacity="0.9"/>'
            f'<stop offset="100%" stop-color="{PALETTE.accent_text}" stop-opacity="0.6"/>'
            "</linearGradient>"
            "</defs>"
            f'<rect width="200" height="200" fill="{PALETTE.bg0}"/>'
            '<rect width="200" height="200" fill="url(#glow)"/>'
            + "".join(bars_svg)
            + "</svg>"
        )

        from PySide6.QtCore import QByteArray, Qt
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtSvg import QSvgRenderer

        size = min(min(w, h), 500)
        svg_data = QByteArray(svg.encode("utf-8"))
        renderer = QSvgRenderer(svg_data)
        if renderer.isValid():
            pix = QPixmap(size, size)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            renderer.render(painter)
            painter.end()
            self.preview_content.setPixmap(pix)

    def _stop_wave_animation(self):
        self._wave_timer.stop()

    def set_cover_art(self, pixmap):
        if self._mode != "video":
            return
        if pixmap is not None and not pixmap.isNull():
            if self._is_audio:
                self._audio_cover_pixmap = pixmap
                if self._expanded:
                    self._display_cover_pixmap(pixmap)
            self._thumb_queue.invalidate(self._playlist_index)
            self._live_thumb_captured = True
            self._thumb_queue.feed_live_cover(self._playlist_index, pixmap)
            self.playlist_panel.set_thumbnail(self._playlist_index, pixmap)
