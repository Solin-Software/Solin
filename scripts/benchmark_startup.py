from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from typing import Any


DEFAULT_BASELINE_MS = 4_056.0
DEFAULT_RUNS = 20
PROFILE_ID = "startup_benchmark"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Solin startup in isolated processes.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help="isolated process samples (20 by default for a meaningful p95)",
    )
    parser.add_argument("--baseline-ms", type=float, default=DEFAULT_BASELINE_MS)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def seed_fixture(root: Path) -> int:
    data_dir = root / "data"
    profile_dir = data_dir / "profiles" / PROFILE_ID
    _write_json(
        data_dir / "profiles.json",
        {
            "profiles": [
                {
                    "id": PROFILE_ID,
                    "name": "Startup Benchmark",
                    "created_at": 1_700_000_000.0,
                }
            ]
        },
    )

    payload = "x" * 3_650
    playlists: list[dict[str, Any]] = []
    media_index = 0
    for playlist_index in range(30):
        items: list[dict[str, Any]] = []
        for item_index in range(20):
            media_index += 1
            items.append(
                {
                    "id": f"media-{media_index}",
                    "title": f"Synthetic media {media_index}",
                    "type": "video",
                    "url": f"https://example.invalid/media/{media_index}.mp4",
                    "duration": 240_000 + item_index,
                    "metadata": {"benchmark_payload": payload},
                }
            )
        playlists.append(
            {
                "id": f"playlist-{playlist_index}",
                "name": f"Synthetic playlist {playlist_index}",
                "items": items,
            }
        )
    playlists_file = profile_dir / "playlists.json"
    _write_json(playlists_file, {"playlists": playlists})
    _write_json(
        profile_dir / "meeting_trees.json",
        {"version": 1, "trees": {}},
    )
    _write_json(profile_dir / "pending_cleanup.json", {"pending": []})
    (root / "cache").mkdir(parents=True, exist_ok=True)
    (root / "settings").mkdir(parents=True, exist_ok=True)
    return playlists_file.stat().st_size


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def _phase_value(sample: dict[str, Any], name: str) -> float:
    phases = sample.get("phases", ())
    for phase in phases:
        if phase.get("name") == name:
            return float(phase["elapsed_ms"])
    raise RuntimeError(f"Benchmark sample did not contain phase {name!r}")


def _run_sample(root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["SOLIN_STARTUP_TRACE"] = "1"
    environment["SOLIN_STARTUP_BENCHMARK_EXIT"] = "1"
    child = Path(__file__).with_name("_startup_benchmark_child.py")
    process = subprocess.run(
        [sys.executable, str(child), str(root)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=120,
    )
    for line in reversed(process.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("schema") == "solin.startup.v1":
            return payload
    raise RuntimeError(
        "Startup benchmark child produced no timeline.\n"
        f"exit={process.returncode}\nstdout={process.stdout}\nstderr={process.stderr}"
    )


def _build_report(
    samples: list[dict[str, Any]],
    *,
    fixture_size: int,
    baseline_ms: float,
) -> dict[str, Any]:
    first_paints = [_phase_value(sample, "first_paint") for sample in samples]
    shell_first_paints = [
        _phase_value(sample, "startup_shell_first_paint") for sample in samples
    ]
    shows = [_phase_value(sample, "show_returned") for sample in samples]
    unit_durations = [
        float(duration)
        for sample in samples
        for duration in sample.get("measurements_ms", {}).get(
            "ui_preparation_units",
            (),
        )
    ]
    paint_p50 = statistics.median(first_paints)
    paint_p95 = _percentile(first_paints, 0.95)
    reduction = ((baseline_ms - paint_p50) / baseline_ms) * 100.0
    unit_p95 = _percentile(unit_durations, 0.95)
    phase_names = (
        "application_imported",
        "container_ready",
        "main_window_imported",
        "critical_ui_ready",
        "show_returned",
        "first_paint",
    )
    phase_summary = {}
    for phase_name in phase_names:
        values = [_phase_value(sample, phase_name) for sample in samples]
        phase_summary[phase_name] = {
            "p50": round(statistics.median(values), 3),
            "p95": round(_percentile(values, 0.95), 3),
        }
    return {
        "schema": "solin.startup-benchmark.v1",
        "runs": len(samples),
        "fixture": {
            "playlists_bytes": fixture_size,
            "media_items": 600,
        },
        "baseline_first_paint_ms": round(baseline_ms, 3),
        "show_ms": {
            "p50": round(statistics.median(shows), 3),
            "p95": round(_percentile(shows, 0.95), 3),
        },
        "startup_shell_first_paint_ms": {
            "samples": [round(value, 3) for value in shell_first_paints],
            "p50": round(statistics.median(shell_first_paints), 3),
            "p95": round(_percentile(shell_first_paints, 0.95), 3),
        },
        "first_paint_ms": {
            "samples": [round(value, 3) for value in first_paints],
            "p50": round(paint_p50, 3),
            "p95": round(paint_p95, 3),
        },
        "reduction_percent": round(reduction, 2),
        "phases_ms": phase_summary,
        "incremental_units_ms": {
            "count": len(unit_durations),
            "p95": round(unit_p95, 3),
            "max": round(max(unit_durations, default=0.0), 3),
        },
        "criteria": {
            "first_paint_p50_at_most_2200_ms": paint_p50 <= 2_200.0,
            "first_paint_p95_at_most_2600_ms": paint_p95 <= 2_600.0,
            "startup_shell_p95_at_most_1000_ms": (
                _percentile(shell_first_paints, 0.95) <= 1_000.0
            ),
            "reduction_at_least_40_percent": reduction >= 40.0,
            "incremental_unit_p95_below_8_ms": unit_p95 < 8.0,
            "incremental_unit_max_at_most_16_ms": max(unit_durations, default=0.0) <= 16.0,
        },
    }


def _print_human_report(report: dict[str, Any]) -> None:
    paint = report["first_paint_ms"]
    shell = report["startup_shell_first_paint_ms"]
    units = report["incremental_units_ms"]
    print(
        f"Startup shell p50/p95: {shell['p50']:.1f} / {shell['p95']:.1f} ms"
    )
    print(
        f"First paint p50/p95: {paint['p50']:.1f} / {paint['p95']:.1f} ms "
        f"({report['reduction_percent']:.1f}% below baseline)"
    )
    print(
        f"Incremental units p95/max: {units['p95']:.1f} / {units['max']:.1f} ms "
        f"across {units['count']} units"
    )
    for criterion, passed in report["criteria"].items():
        print(f"{'PASS' if passed else 'FAIL'} {criterion}")


def main() -> int:
    args = _parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be at least 1")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.fixture_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="solin-startup-benchmark-")
        fixture_root = Path(temporary.name)
    else:
        fixture_root = args.fixture_root.resolve()
        fixture_root.mkdir(parents=True, exist_ok=True)

    fixture_size = seed_fixture(fixture_root)
    samples = [_run_sample(fixture_root) for _ in range(args.runs)]
    report = _build_report(
        samples,
        fixture_size=fixture_size,
        baseline_ms=args.baseline_ms,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
