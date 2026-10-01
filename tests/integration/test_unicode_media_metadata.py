from __future__ import annotations

import shutil
import subprocess

import pytest

from solin.core.media.ffprobe_metadata import probe_tags
from solin.core.media.routed_metadata import RoutedMediaMetadataExtractor


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg/FFprobe are required for the real media metadata round-trip",
)
def test_unicode_file_and_title_reach_player_metadata_signal(tmp_path):
    path = tmp_path / "canção d'água – apresentação.mp4"
    title = 'Imite os fiéis, não “imitações” — ação & paz 🎵'
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=0.1",
            "-metadata", f"title={title}", "-c:v", "mpeg4", str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    tags = probe_tags(str(path))
    assert tags.title == title
    assert tags.has_video

    results = []
    extractor = RoutedMediaMetadataExtractor(runner=lambda work: work())
    extractor.metadata_ready.connect(lambda *args: results.append(args))
    extractor.request(7, str(path))
    assert results == [(7, title, None)]
