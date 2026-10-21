#!/usr/bin/env bash
# S3: remote commenter -> hall speakers -> hall mic -> program -> remote, with and without echo cancellation.
# Needs a LiveKit server on :7880, speakers audible to the hall mic (MIC below).
set -u
cd "$(dirname "$0")/.."
MIC=${MIC:-alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback}
PHASE=${PHASE:-90}
TS=$(date +%Y%m%d_%H%M%S)
HALL=results/s3_hall_${TS}.jsonl
mkdir -p results

LOG_NAME=s3_remote .venv/bin/python s1_publish/probe_server.py > results/s3_server.log 2>&1 &
SRV=$!
sleep 1

phase() {
    local label=$1; shift
    local room="s3-$label-$TS"
    echo "== $label"
    .venv/bin/python s3_audio/hall.py --room "$room" --mic "$MIC" ${PLAY:+--play-target "$PLAY"} --play-gain "${GAIN:-1}" ${ECHO:+--echo-delay-ms "$ECHO" --echo-gain "${ECHO_GAIN:-0.5}"} --duration $((PHASE + 15)) --out "$HALL" "$@" \
        >/dev/null 2>>results/s3_hall.log &
    local hall=$!
    sleep 6
    local prof="$HOME/snap/chromium/common/s3-profile"; rm -rf "$prof"; mkdir -p "$prof"
    chromium --headless=new --user-data-dir="$prof" --autoplay-policy=no-user-gesture-required \
        --remote-debugging-port=9333 --no-first-run "http://127.0.0.1:8090/s3.html?room=$room&label=$label" \
        >/dev/null 2>&1 &
    sleep "$PHASE"
    .venv/bin/python s3_audio/cdp_close.py 9333
    wait "$hall"
    rm -rf "$prof"
}

phase aec-off
phase aec-on --aec
phase aec-ns-on --aec --ns
kill "$SRV"
echo "hall: $HALL  remote: $(ls -t results/s3_remote_*.jsonl | head -1)"
