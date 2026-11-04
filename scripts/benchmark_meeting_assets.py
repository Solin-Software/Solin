"""Measure publication image reuse and global ownership collection with synthetic data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths  # noqa: E402
from solin.core.media.publication_assets import PublicationAssetStore  # noqa: E402
from solin.core.meetings.asset_lifecycle import MeetingAssetLifecycle  # noqa: E402
from solin.core.meetings.content_policy import MeetingContentPolicy  # noqa: E402
from solin.core.playlists.storage import PlaylistRepository  # noqa: E402


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {"p50_ms": round(statistics.median(samples), 3), "p95_ms": round(p95, 3)}


def _measure(action, runs: int) -> dict[str, float]:
    samples = []
    for _ in range(runs):
        start = time.perf_counter_ns()
        action()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return _summary(samples)


def run(*, images: int, size_kib: int, runs: int) -> dict:
    if images < 2 or size_kib < 1 or runs < 5:
        raise ValueError("Use at least two images, one KiB per image, and five runs")
    with tempfile.TemporaryDirectory(prefix="solin-meeting-assets-benchmark-") as directory:
        root = Path(directory)
        runtime = RuntimePaths.from_roots(data_dir=root / "data", cache_dir=root / "cache")
        assets = PublicationAssetStore(runtime.publication_assets_dir)
        source = root / "source.jpg"
        paths = []
        # Payloads exercise hashing and durable filesystem publication, without
        # image decoding, HTTP, or access to any installed profile.
        payload = b"synthetic image bytes" * ((size_kib * 1024 // 21) + 1)
        cold_samples = []
        for index in range(images):
            source.write_bytes(index.to_bytes(8) + payload[:size_kib * 1024 - 8])
            start = time.perf_counter_ns()
            paths.append(assets.materialize(source))
            cold_samples.append((time.perf_counter_ns() - start) / 1_000_000)
        warm = _measure(lambda: assets.materialize(source), runs)
        for profile_id, selected in (("first", paths[::2]), ("second", paths[1::2])):
            profile = ProfilePaths.from_roots(
                data_dir=runtime.data_dir, cache_dir=runtime.cache_dir, profile_id=profile_id,
            )
            profile.ensure_dirs()
            PlaylistRepository(profile.playlists_file).save_strict([{
                "id": profile_id, "name": profile_id,
                "items": [{"id": str(index), "url": path, "type": "image"}
                          for index, path in enumerate(selected)],
            }])
        lifecycle = MeetingAssetLifecycle(runtime, assets, MeetingContentPolicy())
        try:
            def collect_owned() -> None:
                if lifecycle.collect():
                    raise AssertionError("Collection deleted a referenced benchmark image")

            collection = _measure(collect_owned, runs)
        finally:
            lifecycle.shutdown()
        return {
            "platform": platform.system(), "python": platform.python_version(),
            "images": images, "image_kib": size_kib, "runs": runs,
            "cold_publication": _summary(cold_samples),
            "warm_reuse": warm,
            "collect_two_profiles": collection,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=int, default=128)
    parser.add_argument("--size-kib", type=int, default=256)
    parser.add_argument("--runs", type=int, default=15)
    args = parser.parse_args()
    print(json.dumps(run(images=args.images, size_kib=args.size_kib, runs=args.runs), indent=2))


if __name__ == "__main__":
    main()
