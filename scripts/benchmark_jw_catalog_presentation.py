"""Benchmark JW catalog snapshot presentation without network or filesystem I/O."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from solin.core.media.insertion import MediaInsertResult  # noqa: E402
from solin.ui.qml.jw_media_catalog import JWMediaCatalogBridge  # noqa: E402


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)


class _CatalogService:
    def __init__(self, _parent) -> None:
        self.videos_progress = _Signal()
        self.videos_ready = _Signal()
        self.fetch_failed = _Signal()

    def cancel_all(self, *, wait_ms: int = 0) -> None:
        del wait_ms


class _ThumbnailSession:
    def __init__(self) -> None:
        self.ready = _Signal()

    def enqueue(self, _item_id: str, _thumbnail_url: str) -> bool:
        return True

    def reset(self) -> None:
        return

    def close(self) -> None:
        return


class _ThumbnailFactory:
    def create(self, *, parent=None) -> _ThumbnailSession:
        del parent
        return _ThumbnailSession()


def _item(index: int) -> dict:
    return {
        "id": f"id-{index}",
        "title": f"Catalog video {index}",
        "download_url": f"https://cdn.example/{index}.mp4",
        "thumbnail_url": f"https://cdn.example/{index}.jpg",
        "thumbnail_path": "",
        "duration_seconds": 60,
        "duration_ticks": 600_000_000,
        "primary_category": "",
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _run(*, item_count: int, update_count: int) -> dict:
    if item_count <= update_count or update_count <= 0:
        raise ValueError("item-count must be greater than a positive update-count")

    QCoreApplication.instance() or QCoreApplication([])
    bridge = JWMediaCatalogBridge(
        _CatalogService,
        _ThumbnailFactory(),
        insertion_handler=lambda item, _list_id, _index: MediaInsertResult(
            added_items=(item,)
        ),
    )
    bridge._active_catalog_request_id = "benchmark"
    model_resets = 0

    def count_reset() -> None:
        nonlocal model_resets
        model_resets += 1

    bridge._model.modelReset.connect(count_reset)
    catalog = [_item(index) for index in range(item_count)]
    accumulated = list(catalog[: item_count - update_count])
    durations_ms: list[float] = []

    try:
        for completed, new_item in enumerate(
            catalog[item_count - update_count :],
            start=1,
        ):
            accumulated.insert(0, new_item)
            started = time.perf_counter_ns()
            bridge._on_videos_progress(
                "benchmark",
                accumulated,
                completed,
                update_count,
            )
            durations_ms.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
    finally:
        bridge.cleanup()

    p95_ms = _percentile(durations_ms, 0.95)
    max_ms = max(durations_ms)
    return {
        "schema": "solin.jw-catalog-presentation-benchmark.v1",
        "fixture": {
            "items": item_count,
            "progress_updates": update_count,
            "visible_rows": 24,
        },
        "callbacks_ms": {
            "p50": round(statistics.median(durations_ms), 3),
            "p95": round(p95_ms, 3),
            "max": round(max_ms, 3),
        },
        "model_reset_count": model_resets,
        "criteria": {
            "p95_below_8_ms": p95_ms < 8.0,
            "max_below_16_ms": max_ms < 16.0,
            "zero_model_resets": model_resets == 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark progressive JW catalog presentation."
    )
    parser.add_argument("--items", type=int, default=3_938)
    parser.add_argument("--updates", type=int, default=175)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when an acceptance criterion fails",
    )
    args = parser.parse_args()
    result = _run(item_count=args.items, update_count=args.updates)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.strict and not all(result["criteria"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
