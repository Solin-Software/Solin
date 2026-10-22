"""Summarise a probe .jsonl (latency) and optional pidstat file (CPU/RSS per process).

    python3 analyze.py results/g1_x.jsonl [results/g1_x_pidstat.txt]
"""

import json
import statistics
import sys
from collections import defaultdict


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def latency(path):
    rows = [json.loads(line) for line in open(path) if line.strip()]
    print(f"{path}: {len(rows)} matched stimuli, {rows[-1]['t'] / 60:.1f} min")
    for key, name in (("vLat", "video g2g"), ("vRecvLat", "video recv"), ("aLat", "audio g2g"), ("sync", "A-V sync")):
        v = [r[key] for r in rows if r.get(key) is not None]
        print(f"  {name:11} median {statistics.median(v):5.0f}  p5 {pct(v, .05):5.0f}  p95 {pct(v, .95):5.0f}")
    windows = defaultdict(list)
    for r in rows:
        windows[r["t"] // 300].append(r)
    print("  per 5 min (video / audio / sync medians):")
    for w, rs in sorted(windows.items()):
        med = lambda k: statistics.median(r[k] for r in rs)
        print(f"    {w * 5:3}-{w * 5 + 5:<3} min  {med('vLat'):4.0f} / {med('aLat'):4.0f} / {med('sync'):5.0f}  (n={len(rs)})")


def pidstat(path):
    cpu, rss = defaultdict(list), defaultdict(list)
    for line in open(path):
        parts = line.replace(",", ".").split()   # pidstat prints the locale's decimal comma
        if len(parts) < 15 or not parts[1].isdigit():
            continue
        # Time UID PID %usr %system %guest %wait %CPU CPU minflt/s majflt/s VSZ RSS %MEM Command
        cmd = parts[-1]
        cpu[cmd].append(float(parts[7]))
        rss[cmd].append(int(parts[12]) / 1024)
    print(f"{path}:")
    for cmd in cpu:
        c, r = cpu[cmd][1:], rss[cmd]
        print(f"  {cmd:10} CPU mean {statistics.mean(c) / 100:.2f} cores (p95 {pct(c, .95) / 100:.2f})  RSS {r[0]:.0f} -> {r[-1]:.0f} MB")


if __name__ == "__main__":
    latency(sys.argv[1])
    if len(sys.argv) > 2:
        pidstat(sys.argv[2])
