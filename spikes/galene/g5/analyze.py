"""Join remote 'sent' beeps with hall onsets and remote returns; per-leg latency and echo attenuation.

G5 copy of the S3 script: prefers the page's 'sent_seen' time (beep detected in the outgoing stream).
"""
import json, statistics as st, sys
hall = [json.loads(l) for l in open(sys.argv[1])]
remote = [json.loads(l) for l in open(sys.argv[2])]

def med(v): return f"{st.median(v):6.0f}" if v else "     -"
def p(v, q): return f"{sorted(v)[int(len(v)*q)]:6.0f}" if v else "     -"

for label, aec in (("aec-off", (False, False)), ("aec-on", (True, False)), ("aec-ns-on", (True, True))):
    seen = {r["k"]: r["epoch_ms"] for r in remote if r.get("label") == label and r["event"] == "sent_seen"}
    sent = [{**r, "epoch_ms": seen.get(r["k"], r["epoch_ms"])} for r in remote
            if r.get("label") == label and r["event"] == "sent"]
    if not sent:
        continue
    t0, t1 = sent[0]["epoch_ms"] - 2000, sent[-1]["epoch_ms"] + 3000
    h = [r for r in hall if t0 <= r.get("epoch_ms", 0) <= t1 and r["aec"] == aec[0]]
    rx = [r["epoch_ms"] for r in h if r["event"] == "remote_rx"]
    mic = [r["epoch_ms"] for r in h if r["event"] == "mic_raw"]
    echo = [r for r in h if r["event"] == "echo_window"]
    ret = {r["k"]: r for r in remote if r.get("label") == label and r["event"] == "returned"}
    peaks = {r["k"]: r["rms"] for r in remote if r.get("label") == label and r["event"] == "return_peak"}
    legs = {"remote->hall rx": [], "hall rx->mic": [], "remote->mic": [], "round trip": []}
    att = []
    for s in sent:
        e = s["epoch_ms"]
        r1 = next((x for x in rx if 0 < x - e < 1500), None)
        m1 = next((x for x in mic if r1 and 0 < x - r1 < 1500), None)
        if r1: legs["remote->hall rx"].append(r1 - e)
        if r1 and m1: legs["hall rx->mic"].append(m1 - r1); legs["remote->mic"].append(m1 - e)
        if s["k"] in ret and 30 < ret[s["k"]]["rtt_ms"] < 3000: legs["round trip"].append(ret[s["k"]]["rtt_ms"])
        if m1:
            w = next((x for x in echo if abs(x["epoch_ms"] - m1) < 5), None)
            if w: att.append(w["attenuation_db"])
    print(f"\n{label}: {len(sent)} beeps sent, {len(ret)} returns detected, mic onsets matched {len(legs['hall rx->mic'])}")
    for k, v in legs.items():
        print(f"  {k:16} n={len(v):3}  median {med(v)} ms  p10 {p(v,0.1)}  p90 {p(v,0.9)}")
    if att: print(f"  echo attenuation in program (1 kHz band): median {st.median(att):.1f} dB (n={len(att)})")
    rp = list(peaks.values())
    print(f"  returned beep level at remote: median {st.median(rp):.4f}" if rp else "  returned beep level at remote: none detected")
