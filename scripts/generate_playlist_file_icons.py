from __future__ import annotations

import argparse
import io
import os
import struct
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


def _dib_icon_frame(image: Image.Image) -> bytes:
    width, height = image.size
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    xor_bitmap = bytearray()
    mask_stride = ((width + 31) // 32) * 4
    and_mask = bytearray()

    for y in range(height - 1, -1, -1):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            xor_bitmap.extend((blue, green, red, alpha))

        mask_row = bytearray(mask_stride)
        for x in range(width):
            if pixels[x, y][3] == 0:
                mask_row[x // 8] |= 0x80 >> (x % 8)
        and_mask.extend(mask_row)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,
        1,
        32,
        0,
        len(xor_bitmap),
        0,
        0,
        0,
        0,
    )
    return header + xor_bitmap + and_mask


def _ico_bytes(svg: bytes) -> bytes:
    source = _render_svg(svg, 512)
    frames = [
        _dib_icon_frame(source.resize((size, size), Image.Resampling.LANCZOS))
        for size in ICON_SIZES
    ]
    directory_size = 6 + 16 * len(frames)
    offset = directory_size
    entries = bytearray()

    for size, frame in zip(ICON_SIZES, frames, strict=True):
        dimension = 0 if size == 256 else size
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(frame),
                offset,
            )
        )
        offset += len(frame)

    return struct.pack("<HHH", 0, 1, len(frames)) + entries + b"".join(frames)


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
