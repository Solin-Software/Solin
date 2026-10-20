#!/usr/bin/env bash
# Isolate the publisher memory growth seen in the 35-minute webcam run.
# Needs the local server (lk-s1). Each config runs DURATION seconds with 1 subscriber.
set -u
cd "$(dirname "$0")/.."
DURATION=${DURATION:-480}
OUT=results/s1_leak_$(date +%Y%m%d_%H%M%S).jsonl
MIC=alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback
mkdir -p results

run() {
    local label=$1; shift
    echo "== $label ($*)" | tee -a "$OUT.log"
    docker run --rm --network host livekit/livekit-cli load-test \
        --url ws://127.0.0.1:7880 --api-key devkey --api-secret secret --room s1 \
        --subscribers 1 --duration "$((DURATION + 15))s" >>"$OUT.log" 2>&1 &
    local lt=$!
    .venv/bin/python s1_publish/publish_b.py \
        --duration "$DURATION" --interval 60 --out "$OUT" "$@" 2>>"$OUT.log" \
        | grep --line-buffered '"event"' | sed "s/^/[$label] /" | cut -c1-220
    wait "$lt"
    sleep 3
}

case "${ROUND:-1}" in
1)
    run clip-baseline --codec h264 --encoder vaapi --baseline
    run clip-callbacks --codec h264 --encoder vaapi --no-livekit --tracemalloc
    run clip-video --codec h264 --encoder vaapi --no-audio
    run clip-audio --codec h264 --encoder vaapi --no-video
    run clip-full --codec h264 --encoder vaapi --tracemalloc
    run webcam-baseline --codec h264 --encoder vaapi --webcam /dev/video0 --mic "$MIC" --baseline
    ;;
2)
    run vaapi-nosim --codec h264 --encoder vaapi --no-audio --no-simulcast
    run h264-sw --codec h264 --encoder software --no-audio
    run vp8-sw --codec vp8 --encoder software --no-audio
    run vp8-sw-nosim --codec vp8 --encoder software --no-audio --no-simulcast
    ;;
esac
echo "results: $OUT"
