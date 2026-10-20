"""Run a command and sample its /proc/<pid>/smaps RSS grouped by mapping kind.

    python smaps_watch.py --interval 30 -- python leak_repro.py --encoder vaapi --duration 240
"""

import argparse
import collections
import json
import subprocess
import sys
import time


def classify(path: str) -> str:
    if not path:
        return "anon"
    if path.startswith("/dev/dri"):
        return "dri"
    if path in ("[heap]", "[stack]"):
        return path
    if path.startswith("["):
        return "other-special"
    if ".so" in path:
        return "libs"
    return "files"


def sample(pid: int) -> dict[str, int]:
    totals: collections.Counter[str] = collections.Counter()
    kind = "anon"
    with open(f"/proc/{pid}/smaps") as f:
        for line in f:
            parts = line.split()
            if parts and "-" in parts[0] and len(parts) >= 5 and not parts[0].endswith(":"):
                kind = classify(parts[5] if len(parts) > 5 else "")
            elif parts and parts[0] == "Rss:":
                totals[kind] += int(parts[1])
    return {k: v // 1024 for k, v in totals.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start = time.monotonic()
    while proc.poll() is None:
        time.sleep(a.interval)
        try:
            print(json.dumps({"t": round(time.monotonic() - start), "rss_mb": sample(proc.pid)}), flush=True)
        except FileNotFoundError:
            break
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
