from __future__ import annotations

import argparse
import hashlib
import io
import os
import struct
from pathlib import Path

from PIL import Image, PngImagePlugin
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist.svg"
ICO_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist.ico"
PNG_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist-512.png"
PNG_2X_OUTPUT = PROJECT_ROOT / "src" / "solin" / "resources" / "assets" / "playlist-1024.png"
ICON_SIZES = (16, 24, 32, 48, 64, 96, 128, 256)
PNG_SOURCE_DIGEST_KEY = "solin.source.sha256"
PNG_PIXEL_DIGEST_KEY = "solin.pixels.sha256"

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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pixel_digest(image: Image.Image) -> str:
    return _sha256(image.tobytes())


def _png_bytes(image: Image.Image, source_digest: str) -> bytes:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text(PNG_SOURCE_DIGEST_KEY, source_digest)
    metadata.add_text(PNG_PIXEL_DIGEST_KEY, _pixel_digest(image))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True, pnginfo=metadata)
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


def _ico_bytes(source: Image.Image) -> bytes:
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


def _check_png(
    path: Path,
    expected_size: tuple[int, int],
    source_digest: str,
) -> Image.Image | None:
    try:
        with Image.open(path) as actual:
            actual.load()
            valid = (
                actual.format == "PNG"
                and actual.mode == "RGBA"
                and actual.size == expected_size
                and actual.info.get(PNG_SOURCE_DIGEST_KEY) == source_digest
                and actual.info.get(PNG_PIXEL_DIGEST_KEY) == _pixel_digest(actual)
            )
            canonical = actual.copy() if valid else None
    except OSError:
        canonical = None

    if canonical is not None:
        return canonical
    print(f"out of date: {path.relative_to(PROJECT_ROOT)}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate native Solin playlist file icons.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that generated assets match the SVG source without writing files",
    )
    args = parser.parse_args()

    svg = SOURCE.read_bytes()
    source_digest = _sha256(svg)

    if args.check:
        source = _check_png(PNG_OUTPUT, (512, 512), source_digest)
        source_2x = _check_png(PNG_2X_OUTPUT, (1024, 1024), source_digest)
        valid = source is not None and source_2x is not None
        if source is not None:
            valid = _check_file(ICO_OUTPUT, _ico_bytes(source)) and valid
        return 0 if valid else 1

    application = QGuiApplication.instance() or QGuiApplication([])
    source = _render_svg(svg, 512)
    ico = _ico_bytes(source)
    source_2x = _render_svg(svg, 1024)
    ICO_OUTPUT.write_bytes(ico)
    PNG_OUTPUT.write_bytes(_png_bytes(source, source_digest))
    PNG_2X_OUTPUT.write_bytes(_png_bytes(source_2x, source_digest))
    print(f"generated: {ICO_OUTPUT.relative_to(PROJECT_ROOT)}")
    print(f"generated: {PNG_OUTPUT.relative_to(PROJECT_ROOT)}")
    print(f"generated: {PNG_2X_OUTPUT.relative_to(PROJECT_ROOT)}")
    application.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
