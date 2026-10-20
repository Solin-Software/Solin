"""Start leak_repro and report mapping counts / RSS by mapping name every N seconds."""
import collections, subprocess, sys, time
args = sys.argv[1:]
p = subprocess.Popen([".venv/bin/python", "s1_publish/leak_repro.py", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(15)
prev = None
while p.poll() is None:
    c = collections.Counter(); r = collections.Counter(); kind = None
    for l in open(f"/proc/{p.pid}/smaps"):
        f = l.split()
        if f and "-" in f[0] and not f[0].endswith(":"):
            kind = f[5] if len(f) > 5 else "[anon]"; c[kind] += 1
        elif f and f[0] == "Rss:":
            r[kind] += int(f[1])
    snap = {k: (c[k], r[k] // 1024) for k in c}
    top = sorted(snap.items(), key=lambda x: -x[1][1])[:5]
    delta = "" if prev is None else "  growth: " + ", ".join(
        f"{k.split('/')[-1]} {snap[k][0]-prev.get(k,(0,0))[0]:+d} maps {snap[k][1]-prev.get(k,(0,0))[1]:+d} MB"
        for k in snap if abs(snap[k][1] - prev.get(k, (0, 0))[1]) >= 2 or snap[k][0] - prev.get(k, (0, 0))[0] > 5)
    print(f"maps={sum(c.values())}", " ".join(f"{k.split('/')[-1]}:{n}/{mb}MB" for k, (n, mb) in top), delta, flush=True)
    prev = snap
    time.sleep(30)
