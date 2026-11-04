"""Measure local publication inspection, refinement, previews, and selected imports."""

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

from PySide6.QtCore import QCoreApplication  # noqa: E402
from solin.core.jw.jwpub_import import (  # noqa: E402
    JwpubImportRequest,
    JwpubPlaylistImportService,
)
from solin.ui.qml.jwpub_selection import _preview_url  # noqa: E402
from solin.ui.qml.jwpub_selection_model import JwpubSelectionModel  # noqa: E402


def measure(action):
    start = time.perf_counter_ns()
    result = action()
    return result, (time.perf_counter_ns() - start) / 1_000_000


def summary(samples):
    ordered = sorted(samples)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {"p50_ms": round(statistics.median(samples), 3), "p95_ms": round(p95, 3)}


def run(path: Path, runs: int):
    samples = {
        name: []
        for name in (
            "reopen_catalog",
            "select_and_expand",
            "preview_first",
            "preview_cached",
            "materialize_first",
            "materialize_reused",
        )
    }
    service = JwpubPlaylistImportService()
    request = JwpubImportRequest(str(path), resolve_urls=False)
    with service.open(request) as initial:
        catalog = initial.catalog
        first = next(iter(catalog.documents), None)
        if first is None:
            raise ValueError("Publication contains no importable content")
        selected_ids = tuple(item.id for item in first.media)
        preview_id = next((item.id for item in first.media if item.preview_member_path), None)
    with tempfile.TemporaryDirectory(prefix="solin-jwpub-benchmark-") as temporary:
        for index in range(runs):
            publication, elapsed = measure(lambda: service.open(request))
            samples["reopen_catalog"].append(elapsed)
            with publication:
                model = JwpubSelectionModel()
                model.set_catalog(publication.catalog)

                def expand(current=model):
                    current.toggle_document(first.id)
                    current.toggle_expanded(first.id)
                    for row in range(current.rowCount()):
                        current.data(current.index(row), current.ENTRY_ROLE)

                _, elapsed = measure(expand)
                samples["select_and_expand"].append(elapsed)
                if preview_id is not None:
                    for name in ("preview_first", "preview_cached"):
                        _, elapsed = measure(
                            lambda current=publication: _preview_url(current.preview(preview_id))
                        )
                        samples[name].append(elapsed)
                destination = str(Path(temporary) / str(index))
                for name in ("materialize_first", "materialize_reused"):
                    _, elapsed = measure(
                        lambda current=publication, output=destination: current.materialize(
                            selected_ids,
                            dest_images_dir=output,
                            resolve_urls=False,
                        )
                    )
                    samples[name].append(elapsed)
                model.deleteLater()
    return {
        "publication": path.name,
        "archive_bytes": path.stat().st_size,
        "contents": len(catalog.documents),
        "selected_media": len(selected_ids),
        "runs": runs,
        "network": False,
        "measurements": {name: summary(values) for name, values in samples.items() if values},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("publications", nargs="+", type=Path)
    parser.add_argument("--runs", type=int, default=15)
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("Use at least five runs")
    app = QCoreApplication.instance() or QCoreApplication([])
    result = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "note": "Fresh sessions and outputs; operating-system disk cache is not flushed.",
        "publications": [run(path, args.runs) for path in args.publications],
    }
    app.processEvents()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
