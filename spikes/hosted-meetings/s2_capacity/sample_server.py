"""Sample livekit-server CPU/RSS and its Prometheus byte/packet counters as JSON lines.

    python sample_server.py --duration 180 --interval 10 --label speaker50 --out results/s2.jsonl
"""

import argparse
import json
import time
import urllib.request

import psutil


def server_proc() -> psutil.Process:
    for p in psutil.process_iter(["name", "cmdline"]):
        if p.info["name"] == "livekit-server" or "livekit-server" in " ".join(p.info["cmdline"] or [])[:200]:
            if "docker" not in (p.info["name"] or ""):
                return p
    raise SystemExit("livekit-server process not found")


def counters() -> dict[str, float]:
    out: dict[str, float] = {}
    for line in urllib.request.urlopen("http://127.0.0.1:6789/metrics", timeout=3).read().decode().splitlines():
        if line.startswith("livekit_") and ("bytes" in line or "packet" in line or "nack" in line):
            key, _, value = line.rpartition(" ")
            out[key] = float(value)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--label", default="")
    p.add_argument("--out", default="")
    a = p.parse_args()
    proc = server_proc()
    proc.cpu_percent(None)
    prev, prev_t = counters(), time.monotonic()
    start = prev_t
    out = open(a.out, "a") if a.out else None
    while time.monotonic() - start < a.duration:
        time.sleep(a.interval)
        now, t = counters(), time.monotonic()
        dt = t - prev_t
        rate = lambda *parts: sum(v - prev.get(k, 0) for k, v in now.items() if all(x in k for x in parts)) / dt  # noqa: E731
        rec = {"label": a.label, "t": round(t - start), "server_cpu_cores": round(proc.cpu_percent(None) / 100, 2),
               "server_rss_mb": proc.memory_info().rss // 1_000_000,
               "out_mbps": round(rate("livekit_packet_bytes", 'direction="outgoing"') * 8 / 1e6, 2),
               "out_retransmit_mbps": round(rate("livekit_packet_bytes", 'direction="outgoing"', 'transmission="retransmit"') * 8 / 1e6, 2),
               "in_mbps": round(rate("livekit_packet_bytes", 'direction="incoming"') * 8 / 1e6, 2),
               "dropped_pps": round(rate("livekit_node_packet_total", 'type="dropped"'), 1),
               "nack_per_s": round(rate("nack"), 1)}
        line = json.dumps(rec)
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()
        prev, prev_t = now, t


if __name__ == "__main__":
    main()
