from __future__ import annotations

import argparse
import io
import os
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist.svg"
ICO_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist.ico"
PNG_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist-512.png"
PNG_2X_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist-1024.png"
ICON_SIZES = (16, 24, 32, 48, 64, 96, 128, 256)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _render_svg(svg: bytes, size: int) -> Image.Image:
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG source: {SOURCE}")

    image = QImage(QSize(size, size), QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()

    png_data = QByteArray()
    buffer = QBuffer(png_data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
        raise RuntimeError(f"Could not render {SOURCE} at {size}x{size}")
    buffer.close()
    return Image.open(io.BytesIO(bytes(png_data))).convert("RGBA")


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _ico_bytes(svg: bytes) -> bytes:
    source = _render_svg(svg, max(ICON_SIZES))
    output = io.BytesIO()
    source.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return output.getvalue()


def _check_file(path: Path, expected: bytes) -> bool:
    if path.is_file() and path.read_bytes() == expected:
        return True
    print(f"out of date: {path.relative_to(PROJECT_ROOT)}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate native Solin playlist file icons.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that generated assets match the SVG source without writing files",
    )
    args = parser.parse_args()

    svg = SOURCE.read_bytes()
    ico = _ico_bytes(svg)
    png = _png_bytes(_render_svg(svg, 512))
    png_2x = _png_bytes(_render_svg(svg, 1024))

    if args.check:
        valid = (
            _check_file(ICO_OUTPUT, ico)
            and _check_file(PNG_OUTPUT, png)
            and _check_file(PNG_2X_OUTPUT, png_2x)
        )
        return 0 if valid else 1

    ICO_OUTPUT.write_bytes(ico)
    PNG_OUTPUT.write_bytes(png)
    PNG_2X_OUTPUT.write_bytes(png_2x)
    print(f"generated: {ICO_OUTPUT.relative_to(PROJECT_ROOT)}")
    print(f"generated: {PNG_OUTPUT.relative_to(PROJECT_ROOT)}")
    print(f"generated: {PNG_2X_OUTPUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    application = QGuiApplication([])
    raise SystemExit(main())
