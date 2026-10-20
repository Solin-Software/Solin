#!/usr/bin/env bash
# S1 path B encoder matrix. Needs a local server:
#   docker run -d --name lk-s1 --network host livekit/livekit-server --dev --bind 0.0.0.0
# CPUSET emulates a smaller hall PC (default 0-7 = 4 P-cores with HT on this i7-13700T).
set -u
cd "$(dirname "$0")/.."
DURATION=${DURATION:-60}
CPUSET=${CPUSET:-0-7}
OUT=results/s1_matrix_$(date +%Y%m%d_%H%M%S).jsonl
mkdir -p results

run() {
    local label=$1; shift
    echo "== $label ($*) cpuset=$CPUSET" | tee -a "$OUT.log"
    docker run --rm --network host livekit/livekit-cli load-test \
        --url ws://127.0.0.1:7880 --api-key devkey --api-secret secret --room s1 \
        --subscribers 3 --duration "$((DURATION + 15))s" >>"$OUT.log" 2>&1 &
    local lt=$!
    taskset -c "$CPUSET" .venv/bin/python s1_publish/publish_b.py \
        --duration "$DURATION" --interval 10 --out "$OUT" "$@" 2>>"$OUT.log" \
        | sed "s/^/[$label] /" | grep '"event"'
    wait "$lt"
    sleep 3
}

run baseline --baseline
run vp8-sw --codec vp8 --encoder software
run h264-sw --codec h264 --encoder software
run h264-vaapi --codec h264 --encoder vaapi
run vp8-sw-e2ee --codec vp8 --encoder software --e2ee-key spike-shared-key
run h264-vaapi-e2ee --codec h264 --encoder vaapi --e2ee-key spike-shared-key
echo "results: $OUT"
