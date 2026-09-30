"""Qt-free media metadata + thumbnail extraction via the ffprobe/ffmpeg CLIs.

libobs is the only media engine, so QtMultimedia (``QMediaPlayer``/``QMediaMetaData``)
no longer decodes anything. Title, duration, embedded cover art, and thumbnail
frames come from the ``ffprobe`` and ``ffmpeg`` command-line tools instead. These
helpers return plain Python data (``str``/``int``/``bytes``); callers assemble a
``QPixmap``/``QImage`` from the returned image bytes (that is ``QtGui``, not
``QtMultimedia``).

All functions are best-effort and never raise: a missing binary, an unreadable
file, or a timeout yields ``None``/empty so the UI degrades gracefully.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 15.0
_EXTRACT_TIMEOUT_S = 20.0

# Still-image video codecs used for embedded cover art / posters (as opposed to a
# real moving-video stream). A stream in one of these, when the file also carries
# audio or a real video stream, is treated as cover art.
_IMAGE_CODECS = frozenset(
    {"png", "mjpeg", "mjpg", "jpeg", "jpg", "bmp", "gif", "webp", "tiff", "ppm"}
)


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@dataclass(frozen=True)
class MediaTags:
    """Container-level metadata read without decoding."""

    title: str = ""
    duration_ms: int = 0
    has_video: bool = False
    cover_stream_index: int = -1  # ffmpeg stream index of the cover, or -1

    @property
    def has_cover(self) -> bool:
        return self.cover_stream_index >= 0


def probe_tags(path: str) -> MediaTags:
    """Read title, duration, and stream layout via ``ffprobe`` (no decode).

    Returns an empty :class:`MediaTags` if ffprobe is absent or the file cannot be
    parsed.
    """
    if not path or not ffprobe_available():
        return MediaTags()
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:format_tags=title:"
        "stream=index,codec_type,codec_name,disposition",
        "-of", "json", path,
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.debug("ffprobe failed for %r", path, exc_info=True)
        return MediaTags()
    if completed.returncode != 0:
        return MediaTags()
    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError:
        return MediaTags()

    fmt = data.get("format") or {}
    title = ""
    for key, value in (fmt.get("tags") or {}).items():  # tag keys vary in case
        if key.lower() == "title" and isinstance(value, str) and value.strip():
            title = value.strip()
            break

    duration_ms = 0
    try:
        duration_ms = max(0, int(float(fmt.get("duration", 0.0) or 0.0) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0

    attached_pic_index = -1
    image_stream_index = -1
    real_video_index = -1
    has_audio = False
    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        index = stream.get("index")
        if codec_type == "audio":
            has_audio = True
            continue
        if codec_type != "video" or not isinstance(index, int):
            continue
        disposition = stream.get("disposition") or {}
        codec_name = str(stream.get("codec_name") or "").lower()
        if disposition.get("attached_pic"):
            if attached_pic_index < 0:
                attached_pic_index = index
        elif codec_name in _IMAGE_CODECS:
            if image_stream_index < 0:
                image_stream_index = index
        elif real_video_index < 0:
            real_video_index = index

    # An image-codec stream counts as a cover only when the file also carries real
    # content (audio or a moving-video stream); otherwise the file *is* an image.
    cover_index = attached_pic_index
    if cover_index < 0 and image_stream_index >= 0 and (has_audio or real_video_index >= 0):
        cover_index = image_stream_index

    return MediaTags(
        title=title,
        duration_ms=duration_ms,
        has_video=real_video_index >= 0,
        cover_stream_index=cover_index,
    )


def extract_cover(path: str, stream_index: int | None = None) -> bytes | None:
    """Return the embedded cover-art image bytes, or ``None``.

    ``stream_index`` is the ffmpeg stream index of the cover (from
    :func:`probe_tags`); if omitted it is probed. Returns ``None`` when there is no
    cover or ffmpeg is unavailable.
    """
    if not path or not ffmpeg_available():
        return None
    if stream_index is None:
        stream_index = probe_tags(path).cover_stream_index
    if stream_index is None or stream_index < 0:
        return None
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", path,
        "-map", f"0:{stream_index}",
        "-c", "copy", "-f", "image2pipe", "pipe:1",
    ]
    return _run_image(cmd, path, "cover")


def extract_thumbnail(path: str, at_ms: int = 0) -> bytes | None:
    """Decode a single frame at ``at_ms`` and return it as PNG bytes, or ``None``."""
    if not path or not ffmpeg_available():
        return None
    seconds = max(0.0, at_ms / 1000.0)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{seconds:.3f}",
        "-i", path,
        "-frames:v", "1",
        "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ]
    return _run_image(cmd, path, "thumbnail")


def _run_image(cmd: list[str], path: str, kind: str) -> bytes | None:
    try:
        completed = subprocess.run(
            cmd, capture_output=True, timeout=_EXTRACT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.debug("ffmpeg %s extraction failed for %r", kind, path, exc_info=True)
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


__all__ = [
    "MediaTags",
    "ffprobe_available",
    "ffmpeg_available",
    "probe_tags",
    "extract_cover",
    "extract_thumbnail",
]
