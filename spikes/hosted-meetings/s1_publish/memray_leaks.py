"""Print the largest still-allocated (leaked) native stacks from a memray capture."""

import sys

from memray import FileReader

reader = FileReader(sys.argv[1])
meta = reader.metadata
print("total allocations:", meta.total_allocations, "peak:", meta.peak_memory // 1_000_000, "MB")
records = sorted(reader.get_leaked_allocation_records(merge_threads=True), key=lambda r: r.size, reverse=True)
print("still allocated at exit:", sum(r.size for r in records) // 1_000_000, "MB in",
      sum(r.n_allocations for r in records), "allocations,", len(records), "unique stacks")
for rec in records[: int(sys.argv[2]) if len(sys.argv) > 2 else 8]:
    print(f"\n--- {rec.size / 1e6:.1f} MB in {rec.n_allocations} allocations (allocator {rec.allocator})")
    for fn, mod, line in rec.hybrid_stack_trace(max_stacks=20):
        print(f"   {fn[:120]}  [{mod.split('/')[-1]}:{line}]")
