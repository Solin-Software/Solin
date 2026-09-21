"""Measure linked-folder journal, tree projection, and idle hashing costs.

All state lives in temporary directories; no user profile or linked folder is
read or modified. Results describe this machine, not cloud transport latency.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solin.core.ingest.sync.journal import JournalReplica  # noqa: E402
from solin.core.ingest.sync.resources import content_signature  # noqa: E402
from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes  # noqa: E402


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    fraction = position - lower
    p95 = ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * fraction
    return {"p50": round(statistics.median(values), 3), "p95": round(p95, 3)}


def _measure(action: Callable, count: int) -> dict[str, float]:
    values = []
    for _ in range(count):
        start = time.perf_counter_ns()
        action()
        values.append((time.perf_counter_ns() - start) / 1_000_000)
    return _summary(values)


def benchmark(entity_count: int, runs: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="solin-linked-sync-benchmark-") as temporary:
        root = Path(temporary)
        folder = root / "linked"
        folder.mkdir()
        state = root / "local-state"
        nodes = [
            {
                "id": f"node-{index}",
                "type": "media",
                "title": f"Media {index}",
                "media_ref": {"file_path": f"image-{index}.jpg", "mime_type": "image/jpeg"},
                "children": [],
            }
            for index in range(entity_count)
        ]
        entities = flatten_nodes(nodes)
        replica = JournalReplica(folder, "benchmark", state_dir=state, actor_id="benchmark")
        snapshot = replica.read(seed=entities)
        cold_read = _measure(
            lambda: JournalReplica(folder, "benchmark", state_dir=state).read(), runs
        )
        unchanged_read = _measure(replica.read, runs)
        projection = _measure(lambda: rebuild_nodes(snapshot.entities), runs)
        flatten = _measure(lambda: flatten_nodes(nodes, snapshot.entities), runs)

        def edit() -> None:
            nonlocal snapshot
            desired = deepcopy(snapshot.entities)
            desired["node-0"]["title"] = f"Edited {time.perf_counter_ns()}"
            snapshot = replica.commit(snapshot, desired)

        edits = _measure(edit, runs)
        after_edits_read = _measure(replica.read, runs)

        media = root / "media"
        media.mkdir()
        paths = []
        for index in range(entity_count):
            path = media / f"image-{index}.jpg"
            path.write_bytes(b"synthetic image content" * 16)
            paths.append(path)
            content_signature(path)
        original_digest = hashlib.file_digest
        hashes = 0

        def count_hashes(*args, **kwargs):
            nonlocal hashes
            hashes += 1
            return original_digest(*args, **kwargs)

        with patch("hashlib.file_digest", count_hashes):
            idle_hashes = _measure(lambda: [content_signature(path) for path in paths], runs)
        return {
            "entities": entity_count,
            "runs": runs,
            "milliseconds": {
                "cold_read": cold_read,
                "unchanged_read": unchanged_read,
                "tree_rebuild": projection,
                "tree_flatten": flatten,
                "single_field_edit": edits,
                "unchanged_read_after_edits": after_edits_read,
                "idle_content_signatures": idle_hashes,
            },
            "idle_content_hashes_computed": hashes,
            "operations_after_edits": len(snapshot.operation_ids),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--runs", type=int, default=10)
    arguments = parser.parse_args()
    if arguments.runs < 2 or any(size < 1 for size in arguments.sizes):
        parser.error("At least two runs and positive entity counts are required")
    for size in arguments.sizes:
        print(json.dumps(benchmark(size, arguments.runs), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
