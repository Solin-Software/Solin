"""Recovery timeline per client relative to the moment node A was killed."""
import json, statistics as st, sys
rs = [json.loads(l) for l in open(sys.argv[1])]
kill = next(r["t"] for r in rs if r["event"] == "kill_node_a")
by = {}
for r in rs:
    if "who" in r and r["t"] >= kill:
        by.setdefault(r["who"], []).append(r)
def first(evs, kind, node=None):
    return next((e["t"] - kill for e in evs if e["event"] == kind and (node is None or e.get("node") == node)), None)
hall = by.get("hall", [])
detect = lambda evs: min([x for x in (first(evs,'reconnecting'), first(evs,'disconnected'), first(evs,'probe_failed')) if x is not None], default=None)
print(f"hall: detects {detect(hall)} ms, on B {first(hall,'failed_over')} ms, publishing {first(hall,'published',1)} ms")
det, sw, aud = [], [], []
for who, evs in sorted(by.items()):
    if who == "hall":
        continue
    d = detect(evs); s = first(evs, "failed_over"); a = first(evs, "audio", 1)
    det.append(d); sw.append(s); aud.append(a)
fmt = lambda v: f"median {st.median([x for x in v if x is not None]):.0f} ms, max {max(x for x in v if x is not None):.0f} ms, n={sum(x is not None for x in v)}/{len(v)}" if any(x is not None for x in v) else "none"
print("attendees detect failure:", fmt(det)); print("attendees connected to B:", fmt(sw)); print("attendees hear program again:", fmt(aud))
