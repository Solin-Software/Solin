"""Benchmark pure removal planning and filtering with synthetic meeting trees."""

from __future__ import annotations

import argparse
from copy import deepcopy
from statistics import median, quantiles
from time import perf_counter

from solin.core.meetings.media_nodes import additional_meeting_media
from solin.core.media.removal import (
    media_counts_by_container,
    select_media_ids,
    without_media,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()
    if args.runs < 2 or any(size < 1 for size in args.sizes):
        parser.error("sizes must be positive and runs must be at least two")
    print("media\tP50 ms\tP95 ms")
    for size in args.sizes:
        nodes = [{
            "id": f"section-{start}", "type": "section", "children": [{
                "id": str(index), "type": "media", "meeting_generated": index % 3 == 0,
            } for index in range(start, min(start + 100, size))],
        } for start in range(0, size, 100)]
        elapsed = []
        for _ in range(args.runs):
            tree = deepcopy(nodes)
            start = perf_counter()
            media_counts_by_container(tree, eligible=additional_meeting_media)
            selected = select_media_ids(tree, eligible=additional_meeting_media)
            without_media(tree, set(selected))
            elapsed.append((perf_counter() - start) * 1000)
        print(f"{size}\t{median(elapsed):.3f}\t{quantiles(elapsed, n=20)[18]:.3f}")


if __name__ == "__main__":
    main()
