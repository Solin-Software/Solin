"""Group still-allocated records by size bucket and by the first symbolized native frames."""
import collections, sys
from memray import FileReader
recs = list(FileReader(sys.argv[1]).get_leaked_allocation_records(merge_threads=True))
by_bucket = collections.Counter(); by_bucket_n = collections.Counter()
by_stack = collections.Counter(); by_stack_n = collections.Counter()
for r in recs:
    b = 1 << max(0, (r.size // max(r.n_allocations, 1)).bit_length())
    by_bucket[b] += r.size; by_bucket_n[b] += r.n_allocations
    names = [fn for fn, _, _ in r.native_stack_trace(max_stacks=40) if fn != "<unknown>"][:6]
    key = " <- ".join(names) or "<all unknown>"
    by_stack[key] += r.size; by_stack_n[key] += r.n_allocations
print("size bucket (avg alloc <=)   total MB   count")
for b in sorted(by_bucket): print(f"{b:>12}  {by_bucket[b]/1e6:9.1f}  {by_bucket_n[b]:7}")
print("\ntop stacks by retained size")
for k, v in by_stack.most_common(12): print(f"{v/1e6:7.1f} MB {by_stack_n[k]:6} allocs  {k[:230]}")
