"""Headless qualification of the delivered media extraction client and helper."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

MEDIA_RUNTIME_CHECKS = ("metadata", "frame", "video_cover", "audio_cover", "absence")
_SCHEMA = "solin.media-runtime.v1"


def _verify(fixtures: Path) -> None:
    from solin.core.media.media_extract import (
        ImagePolicy,
        MediaExtractRequest,
        MediaExtractService,
    )

    service = MediaExtractService()
    try:
        metadata = service.extract(
            MediaExtractRequest(str(fixtures / "video.mp4"), image_policy=ImagePolicy.NONE)
        )
        if (
            metadata.title != "Canção — ação 🎵"
            or metadata.duration_ms != 2000
            or not metadata.has_video
            or metadata.image is not None
        ):
            raise RuntimeError("The delivered media client did not extract Unicode metadata.")

        for filename, policy, has_video in (
            ("video.mp4", ImagePolicy.COVER_OR_FRAME, True),
            ("video-cover.mp4", ImagePolicy.COVER_ONLY, True),
            ("audio-cover.mp3", ImagePolicy.COVER_ONLY, False),
        ):
            result = service.extract(MediaExtractRequest(str(fixtures / filename), policy))
            image = result.image
            expected_kind = "frame" if policy is ImagePolicy.COVER_OR_FRAME else "cover"
            if (
                image is None
                or image.kind != expected_kind
                or result.has_video is not has_video
                or result.duration_ms <= 0
            ):
                raise RuntimeError(f"The delivered media client did not qualify {filename}.")
            offset = (image.height // 2) * image.stride + (image.width // 2) * 4
            blue, green, red, alpha = image.data[offset : offset + 4]
            if (
                blue >= 25
                or alpha != 255
                or (expected_kind == "frame" and (red <= 230 or green >= 25))
                or (expected_kind == "cover" and (green <= 190 or red >= 25))
            ):
                raise RuntimeError(f"The delivered media client returned wrong pixels: {filename}.")

        absence = service.extract(
            MediaExtractRequest(str(fixtures / "audio.mp3"), ImagePolicy.COVER_ONLY)
        )
        if absence.image is not None or absence.has_video or absence.duration_ms <= 0:
            raise RuntimeError("The delivered media client did not report absent cover art.")
    finally:
        service.close()
    if service.active_count != 0:
        raise RuntimeError("Media qualification left a native extraction process running.")


def main(arguments: Sequence[str]) -> int:
    """Qualify without importing Qt, creating user state or starting playback."""
    result: dict[str, object] = {"schema": _SCHEMA, "ok": False}
    exit_code = 1
    try:
        if len(arguments) != 2 or arguments[0] != "--verify-media-runtime":
            raise ValueError("Usage: --verify-media-runtime <fixture-directory>")
        fixtures = Path(arguments[1]).resolve(strict=True)
        if not fixtures.is_dir():
            raise ValueError("Media qualification requires a fixture directory.")
        for filename in ("video.mp4", "video-cover.mp4", "audio-cover.mp3", "audio.mp3"):
            if not (fixtures / filename).is_file():
                raise ValueError(f"Media qualification fixture is missing: {filename}")
    except (ValueError, OSError) as error:
        result["error"] = type(error).__name__
        print(str(error), file=sys.stderr, flush=True)
        exit_code = 2
    else:
        try:
            _verify(fixtures)
        except Exception as error:  # noqa: BLE001 - isolated qualification process boundary includes import failures
            result["error"] = type(error).__name__
            print(str(error), file=sys.stderr, flush=True)
        else:
            result.update(ok=True, checks=MEDIA_RUNTIME_CHECKS)
            exit_code = 0
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return exit_code
