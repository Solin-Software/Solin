"""Reusable SVG image provider for QML surfaces."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer

from solin.styles.theme import PALETTE


class SvgIconProvider(QQuickImageProvider):
    """Render named ``currentColor`` SVG sources requested by QML."""

    def __init__(
        self,
        icons: Mapping[str, object],
        *,
        default_icon: str,
    ) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self._icons = {name: source for name, source in icons.items() if isinstance(source, str)}
        if default_icon not in self._icons:
            raise ValueError(f"Missing default SVG icon: {default_icon}")
        self._default_icon = default_icon

    def requestPixmap(self, id_str: str, size, requested_size):  # noqa: N802
        del size, requested_size
        parts = id_str.split("/")
        name = parts[0] if parts else self._default_icon
        pixel_size = int(parts[1]) if len(parts) > 1 else 16
        color = f"#{parts[2]}" if len(parts) > 2 else PALETTE.text_muted
        svg_source = self._icons.get(name) or self._icons[self._default_icon]
        renderer = QSvgRenderer(
            QByteArray(svg_source.replace("currentColor", color).encode("utf-8"))
        )

        pixmap = QPixmap(pixel_size, pixel_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        if renderer.isValid():
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
        return pixmap


__all__ = ["SvgIconProvider"]
