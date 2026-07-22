"""Blocking filesystem probe executed only by media-operation workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import stat

from solin.core.media.download_storage import completed_cached_path, is_remote_url


class ProbedMediaAvailability(StrEnum):
    AVAILABLE = "available"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class MediaPresentationProbeRequest:
    source: str
    media_cache_dir: Path
    thumbnail_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MediaPresentationProbeResult:
    availability: ProbedMediaAvailability
    local_path: str = ""
    cached: bool = False
    thumbnail_exists: bool = False
    error: str = ""


def probe_media_presentation(
    request: MediaPresentationProbeRequest,
) -> MediaPresentationProbeResult:
    """Resolve local/cache/thumb state without touching any QObject or QPixmap."""

    thumbnail_exists = _is_regular_file(request.thumbnail_path)
    source = request.source
    if not source:
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.MISSING,
            thumbnail_exists=thumbnail_exists,
        )
    if is_remote_url(source):
        try:
            cached_path = completed_cached_path(source, request.media_cache_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            return MediaPresentationProbeResult(
                ProbedMediaAvailability.AVAILABLE,
                thumbnail_exists=thumbnail_exists,
                error=str(exc),
            )
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.AVAILABLE,
            local_path=cached_path or "",
            cached=cached_path is not None,
            thumbnail_exists=thumbnail_exists,
        )

    try:
        source_stat = os.stat(source)
    except FileNotFoundError:
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.MISSING,
            thumbnail_exists=thumbnail_exists,
        )
    except OSError as exc:
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.TEMPORARILY_UNAVAILABLE,
            thumbnail_exists=thumbnail_exists,
            error=str(exc),
        )
    if not stat.S_ISREG(source_stat.st_mode):
        return MediaPresentationProbeResult(
            ProbedMediaAvailability.MISSING,
            thumbnail_exists=thumbnail_exists,
        )
    return MediaPresentationProbeResult(
        ProbedMediaAvailability.AVAILABLE,
        local_path=os.path.abspath(source),
        thumbnail_exists=thumbnail_exists,
    )


def _is_regular_file(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False
