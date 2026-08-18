"""Benchmark the large-collection paths used by the unified Library."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

from PySide6.QtCore import QCoreApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from solin.core.media.cache_listing import scan_cached_media_items  # noqa: E402
from solin.ui.qml.library import (  # noqa: E402
    DownloadedMediaModel,
    DownloadedMediaProxyModel,
)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _create_fixture(cache_dir: Path, item_count: int) -> None:
    extensions = ("mp4", "m4a", "jpg")
    for index in range(item_count):
        extension = extensions[index % len(extensions)]
        path = cache_dir / f"library-item-{index:05d}.{extension}"
        path.write_bytes(b"fixture")
        Path(f"{path}.done").write_text(
            f"https://cdn.example/library-item-{index:05d}.{extension}",
            encoding="utf-8",
        )


def _run(*, item_count: int, runs: int) -> dict:
    if item_count < 128:
        raise ValueError("items must be at least 128")
    if runs < 5:
        raise ValueError("runs must be at least 5")

    _app = QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory(prefix="solin-library-benchmark-") as temp_dir:
        cache_dir = Path(temp_dir)
        _create_fixture(cache_dir, item_count)
        scan_cached_media_items(cache_dir)

        scan_ms: list[float] = []
        first_batch_ms: list[float] = []
        latest_items = []
        for _ in range(runs):
            started = time.perf_counter_ns()
            batch_arrival: list[float] = []

            def on_batch(
                _items,
                arrivals=batch_arrival,
                scan_started=started,
            ) -> None:
                if not arrivals:
                    arrivals.append((time.perf_counter_ns() - scan_started) / 1_000_000)

            latest_items = scan_cached_media_items(cache_dir, on_batch=on_batch)
            scan_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            first_batch_ms.append(batch_arrival[0])

        source = DownloadedMediaModel()
        proxy = DownloadedMediaProxyModel()
        proxy.setSourceModel(source)
        source.set_items(latest_items, set())

        filter_ms: list[float] = []
        for index in range(runs):
            query = "item-000" if index % 2 == 0 else "item-019"
            started = time.perf_counter_ns()
            proxy.set_query(query)
            proxy.rowCount()
            filter_ms.append((time.perf_counter_ns() - started) / 1_000_000)

        selection_paths = {item.path for item in latest_items[::2]}
        selection_ms: list[float] = []
        for index in range(runs):
            selected = selection_paths if index % 2 == 0 else set()
            started = time.perf_counter_ns()
            source.set_selected_paths(selected)
            selection_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    scan_summary = _summary(scan_ms)
    first_batch_summary = _summary(first_batch_ms)
    filter_summary = _summary(filter_ms)
    selection_summary = _summary(selection_ms)
    return {
        "schema": "solin.library-benchmark.v1",
        "fixture": {"items": item_count, "runs": runs, "batch_size": 128},
        "scan_ms": scan_summary,
        "first_batch_ms": first_batch_summary,
        "filter_ms": filter_summary,
        "half_collection_selection_ms": selection_summary,
        "criteria": {
            "scan_p95_below_1000_ms": scan_summary["p95"] < 1_000.0,
            "first_batch_p95_below_500_ms": first_batch_summary["p95"] < 500.0,
            "filter_p95_below_50_ms": filter_summary["p95"] < 50.0,
            "selection_p95_below_50_ms": selection_summary["p95"] < 50.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Library cache scanning, filtering, and selection."
    )
    parser.add_argument("--items", type=int, default=3_000)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when an acceptance criterion fails",
    )
    args = parser.parse_args()
    result = _run(item_count=args.items, runs=args.runs)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.strict and not all(result["criteria"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
