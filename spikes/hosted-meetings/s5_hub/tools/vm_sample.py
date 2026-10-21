#!/usr/bin/env python3
"""Run on the hub VM (stdlib only): CPU cores per hub process and NIC throughput, as JSON lines.

    python3 vm_sample.py --iface enp0s6 --interval 10 --duration 600 > sample.jsonl
"""

import argparse
import json
import os
import time

NAMES = ("turnserver", "frps", "caddy", "livekit-server")
TICK = os.sysconf("SC_CLK_TCK")


def proc_ticks() -> dict[str, int]:
    out: dict[str, int] = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        name = stat[stat.index("(") + 1: stat.rindex(")")]
        if name in NAMES:
            fields = stat[stat.rindex(")") + 2:].split()
            out[name] = out.get(name, 0) + int(fields[11]) + int(fields[12])   # utime + stime
    return out


def nic_bytes(iface: str) -> tuple[int, int]:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                v = line.split(":", 1)[1].split()
                return int(v[0]), int(v[8])
    raise SystemExit(f"interface {iface} not found")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--iface", required=True)
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--duration", type=float, default=600)
    a = p.parse_args()
    t0 = time.monotonic()
    prev_t, prev_p, prev_n = t0, proc_ticks(), nic_bytes(a.iface)
    while time.monotonic() - t0 < a.duration:
        time.sleep(a.interval)
        t, procs, nic = time.monotonic(), proc_ticks(), nic_bytes(a.iface)
        dt = t - prev_t
        print(json.dumps({
            "t": round(t - t0), "load1": os.getloadavg()[0],
            "cpu_cores": {k: round((v - prev_p.get(k, 0)) / TICK / dt, 3) for k, v in procs.items()},
            "rx_mbps": round((nic[0] - prev_n[0]) * 8 / dt / 1e6, 2),
            "tx_mbps": round((nic[1] - prev_n[1]) * 8 / dt / 1e6, 2),
        }), flush=True)
        prev_t, prev_p, prev_n = t, procs, nic


if __name__ == "__main__":
    main()
