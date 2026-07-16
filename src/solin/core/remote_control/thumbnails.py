"""Safe on-demand thumbnails for private local media."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PureWindowsPath
from typing import Any, Final
from urllib.parse import urlsplit
import warnings

from PIL import Image, ImageDraw, ImageOps

from solin.core.foundation.constants import THUMB_JPEG_QUALITY
from solin.core.media.formats import MediaKind, media_kind_from_mime, media_kind_from_path


_MAX_SOURCE_BYTES: Final = 100 * 1024 * 1024
_MAX_THUMBNAIL_SIZE: Final = (640, 360)
_THUMBNAIL_BACKGROUND: Final = (15, 20, 27)


@dataclass(frozen=True, slots=True)
class PrivateMediaThumbnailSource:
    """Private media reference retained behind the desktop boundary."""

    location: str
    extraction_kind: MediaKind
    placeholder_kind: MediaKind
    is_remote: bool


def private_media_thumbnail_source(
    raw: Mapping[str, Any],
) -> PrivateMediaThumbnailSource | None:
    """Resolve stored media for internal use, never for public serialization."""

    media_ref_value = raw.get("media_ref")
    media_ref = media_ref_value if isinstance(media_ref_value, Mapping) else {}
    location = str(
        raw.get("url") or raw.get("resolved_url") or media_ref.get("file_path") or ""
    ).strip()
    declared_type = str(raw.get("media_type") or raw.get("type") or "").casefold()
    try:
        declared_kind = MediaKind(declared_type)
    except ValueError:
        declared_kind = MediaKind.UNKNOWN
    mime_kind = media_kind_from_mime(str(media_ref.get("mime_type") or ""))
    path_kind = media_kind_from_path(location)
    media_kind = next(
        (kind for kind in (declared_kind, mime_kind, path_kind) if kind is not MediaKind.UNKNOWN),
        MediaKind.UNKNOWN,
    )
    for artwork in (
        raw.get("thumbnail_local_path"),
        raw.get("thumbnail_url"),
    ):
        source = _thumbnail_source(
            str(artwork or ""),
            extraction_kind=MediaKind.IMAGE,
            placeholder_kind=(
                media_kind if media_kind is not MediaKind.UNKNOWN else MediaKind.IMAGE
            ),
        )
        if source is not None and (source.is_remote or Path(source.location).is_file()):
            return source

    return _thumbnail_source(
        location,
        extraction_kind=media_kind,
        placeholder_kind=media_kind,
    )


def _thumbnail_source(
    location: str,
    *,
    extraction_kind: MediaKind,
    placeholder_kind: MediaKind,
) -> PrivateMediaThumbnailSource | None:
    location = location.strip()
    if (
        not location
        or extraction_kind is MediaKind.UNKNOWN
        or placeholder_kind is MediaKind.UNKNOWN
    ):
        return None
    parsed = urlsplit(location)
    is_remote = parsed.scheme.casefold() in ("http", "https")
    is_windows_path = bool(PureWindowsPath(location).drive)
    if parsed.scheme and not is_remote and not is_windows_path:
        return None
    if is_remote and not parsed.netloc:
        return None
    return PrivateMediaThumbnailSource(
        location,
        extraction_kind,
        placeholder_kind,
        is_remote,
    )


def render_local_image_thumbnail(path: str | Path) -> bytes | None:
    """Decode a bounded local image and return a small, normalized JPEG."""

    source_path = Path(path)
    try:
        if not source_path.is_file() or source_path.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source_path) as source:
                return _normalize_image_thumbnail(source)
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def render_image_thumbnail_bytes(data: bytes) -> bytes | None:
    """Normalize bounded in-memory artwork to the remote JPEG contract."""

    if not data or len(data) > _MAX_SOURCE_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                return _normalize_image_thumbnail(source)
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def _normalize_image_thumbnail(source: Image.Image) -> bytes | None:
    source.draft("RGB", _MAX_THUMBNAIL_SIZE)
    image = ImageOps.exif_transpose(source)
    image.thumbnail(_MAX_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
    if image.width <= 0 or image.height <= 0:
        return None
    if "A" in image.getbands():
        normalized = Image.new("RGB", image.size, _THUMBNAIL_BACKGROUND)
        normalized.paste(image, mask=image.getchannel("A"))
    else:
        normalized = image.convert("RGB")
    return _jpeg_bytes(normalized)


def render_media_placeholder_thumbnail(media_kind: MediaKind) -> bytes | None:
    """Render a deterministic visual when a media file has no usable artwork."""

    if media_kind not in (MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.IMAGE):
        return None
    width, height = _MAX_THUMBNAIL_SIZE
    accent = {
        MediaKind.AUDIO: (126, 87, 255),
        MediaKind.VIDEO: (42, 132, 255),
        MediaKind.IMAGE: (29, 185, 146),
    }[media_kind]
    gradient = Image.linear_gradient("L").resize((width, height))
    image = ImageOps.colorize(
        gradient,
        black=_THUMBNAIL_BACKGROUND,
        white=tuple(max(0, channel // 3) for channel in accent),
    ).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse((365, -145, 710, 200), fill=(*accent, 34))
    draw.ellipse((-170, 215, 195, 580), fill=(*accent, 22))
    panel = (190, 70, 450, 290)
    draw.rounded_rectangle(
        panel,
        radius=42,
        fill=(9, 14, 22, 150),
        outline=(*accent, 125),
        width=3,
    )
    if media_kind is MediaKind.AUDIO:
        _draw_audio_symbol(draw, accent)
    elif media_kind is MediaKind.VIDEO:
        _draw_video_symbol(draw, accent)
    else:
        _draw_image_symbol(draw, accent)
    return _jpeg_bytes(Image.alpha_composite(image, overlay).convert("RGB"))


def _draw_audio_symbol(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    bars = (34, 62, 92, 126, 92, 62, 34)
    start_x = 236
    for index, bar_height in enumerate(bars):
        x = start_x + index * 28
        top = 180 - bar_height // 2
        draw.rounded_rectangle(
            (x, top, x + 12, top + bar_height),
            radius=6,
            fill=(*accent, 230),
        )


def _draw_video_symbol(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(
        (237, 123, 403, 237),
        radius=22,
        outline=(*accent, 230),
        width=8,
    )
    draw.polygon(((307, 148), (307, 212), (357, 180)), fill=(*accent, 235))


def _draw_image_symbol(draw: ImageDraw.ImageDraw, accent: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(
        (237, 118, 403, 242),
        radius=22,
        outline=(*accent, 230),
        width=8,
    )
    draw.ellipse((271, 143, 293, 165), fill=(*accent, 235))
    draw.polygon(
        ((253, 219), (302, 172), (330, 198), (352, 178), (388, 219)),
        fill=(*accent, 210),
    )


def _jpeg_bytes(image: Image.Image) -> bytes | None:
    output = BytesIO()
    image.save(
        output,
        "JPEG",
        quality=THUMB_JPEG_QUALITY,
        optimize=True,
    )
    return output.getvalue() or None
