#!/usr/bin/env bash
# S6: hard-kill the primary edge node mid-meeting and time recovery on the backup.
# Usage: STRATEGY=eager|wait ./run_s6.sh
set -u
cd "$(dirname "$0")"
PY=../.venv/bin/python
STRATEGY=${STRATEGY:-eager}
OUT=../results/s6_${STRATEGY}_$(date +%H%M%S).jsonl
NODES="ws://127.0.0.1:7880 ws://127.0.0.1:7890"

docker rm -f lk-s6-a lk-s6-b >/dev/null 2>&1
docker run -d --name lk-s6-a --network host -v "$PWD/node-a.yaml:/n.yaml:ro" livekit/livekit-server --config /n.yaml >/dev/null
docker run -d --name lk-s6-b --network host -v "$PWD/node-b.yaml:/n.yaml:ro" livekit/livekit-server --config /n.yaml >/dev/null
sleep 3

$PY failover.py hall --nodes $NODES --strategy "$STRATEGY" ${PROBE:+--probe} --duration 100 --out "$OUT" >/dev/null 2>&1 &
sleep 4
# One process per attendee, like independent browsers.
for i in $(seq 0 9); do
    $PY failover.py attendees --n 1 --first "$i" --nodes $NODES --strategy "$STRATEGY" ${PROBE:+--probe} --duration 95 \
        --out "$OUT" >/dev/null 2>&1 &
done
sleep 30
echo "{\"event\": \"kill_node_a\", \"t\": $(date +%s%3N)}" >> "$OUT"
if [ "${KILL_MODE:-kill}" = pause ]; then docker pause lk-s6-a >/dev/null; else docker kill lk-s6-a >/dev/null; fi
wait
docker rm -f lk-s6-a lk-s6-b >/dev/null 2>&1
echo "$OUT"
