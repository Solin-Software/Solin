#!/usr/bin/env bash
# S5 tests 3+4: 2.5 h signaling soak through the hub, with hall tunnel cuts of 10/30/60 s
# at minutes 20/40/60 (frpc stopped, then started). Detached-safe.
set -u
cd "$(dirname "$0")/../.."
HOURS=${HOURS:-2.5}
SECS=$(python3 -c "print(int($HOURS*3600))")
LOG=results/s5gcp_soak_events.log
: > "$LOG"
ssh -o BatchMode=yes solin-hub 'awk "/ens4:/{print \$10}" /proc/net/dev' > results/s5gcp_soak_tx_before.txt
.venv/bin/python s1_publish/publish_b.py --url ws://127.0.0.1:7880 --room s5 --codec h264 --encoder software \
    --duration $((SECS + 300)) --interval 300 --out results/s5gcp_soak_pub.jsonl >/dev/null 2>results/s5gcp_soak_pub.log &
sleep 10
.venv/bin/python s5_hub/tools/signal_soak.py --url ws://35.247.228.99 --room s5 --hours "$HOURS" --interval 30 \
    --out results/s5gcp_soak.jsonl >/dev/null 2>results/s5gcp_soak.err &
SOAK=$!
cut() {  # minute, seconds
    sleep $(( $1 * 60 - ${PREV:-0} )); PREV=$(( $1 * 60 + $2 ))
    echo "$(date +%H:%M:%S) cut start ${2}s" >> "$LOG"; docker stop -t 1 s5-frpc >/dev/null
    sleep "$2"
    docker start s5-frpc >/dev/null; echo "$(date +%H:%M:%S) cut end" >> "$LOG"
}
cut 20 10; cut 40 30; cut 60 60
wait "$SOAK"
ssh -o BatchMode=yes solin-hub 'awk "/ens4:/{print \$10}" /proc/net/dev' > results/s5gcp_soak_tx_after.txt
echo done > results/s5gcp_soak.done
