#!/usr/bin/env bash
# S2: hall capacity. Publisher = S1 path B (software H.264, simulcast) pinned to 8 threads;
# N simulated subscribers via lk load-test; server CPU + egress from Prometheus.
# Needs: docker run -d --name lk-s2 --network host -v $PWD/s2_capacity/livekit.yaml:/livekit.yaml \
#          livekit/livekit-server --config /livekit.yaml
set -u
cd "$(dirname "$0")/.."
SAMPLE=${SAMPLE:-120}
TS=$(date +%Y%m%d_%H%M%S)
OUT=results/s2_${TS}.jsonl
PUB=results/s2_${TS}_publisher.jsonl
mkdir -p results

run() {
    local label=$1 subs=$2 layout=$3; shift 3
    local room="s2-$label"
    echo "== $label subs=$subs layout=$layout extra=[$*]" | tee -a "$OUT.log"
    taskset -c 0-7 .venv/bin/python s1_publish/publish_b.py --room "$room" --codec h264 --encoder software \
        --duration $((SAMPLE + 45)) --interval 30 --out "$PUB" ${PUBARGS:-} >/dev/null 2>>"$OUT.log" &
    local pub=$!
    sleep 8
    docker run --rm --network host livekit/livekit-cli load-test --url ws://127.0.0.1:7880 \
        --api-key devkey --api-secret secret --room "$room" --subscribers "$subs" --layout "$layout" \
        --num-per-second 10 --duration "$((SAMPLE + 25))s" "$@" >>"$OUT.log" 2>&1 &
    local lt=$!
    sleep 20
    .venv/bin/python s2_capacity/sample_server.py --duration "$SAMPLE" --interval 15 --label "$label" --out "$OUT" \
        | cut -c1-200
    wait "$lt"; wait "$pub"
    sleep 5
}

PUBARGS="--no-video" run audio50 50 speaker
run v720_50 50 speaker
run v360_50 50 3x3
run v180_50 50 5x5
run cam_50 50 speaker --video-publishers 1 --video-resolution medium
run v720_100 100 speaker
echo "results: $OUT"
