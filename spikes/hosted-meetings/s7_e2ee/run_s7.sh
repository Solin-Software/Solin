#!/usr/bin/env bash
# S7: E2EE between the S1 Python publisher and headless browsers.
# Needs a LiveKit server on :7880 (lk-s2 or lk-s1).
set -u
cd "$(dirname "$0")/.."
KEY=${KEY:-s7-shared-key-$(date +%s)}
ROOM=s7-$(date +%H%M%S)
PER_RUN=${PER_RUN:-45}
mkdir -p results

LOG_NAME=s7 .venv/bin/python s1_publish/probe_server.py > results/s7_server.log 2>&1 &
SRV=$!
.venv/bin/python s1_publish/publish_b.py --room "$ROOM" --codec h264 --encoder software --e2ee-key "$KEY" \
    --duration 900 --interval 30 --out results/s7_publisher.jsonl >/dev/null 2>results/s7_publisher.log &
PUB=$!
sleep 8

url() { echo "http://127.0.0.1:8090/e2ee.html?mode=$1&room=$ROOM#k=$KEY"; }

run_firefox() {
    local prof="$HOME/snap/firefox/common/s7-profile-$1"; rm -rf "$prof"; mkdir -p "$prof"
    printf 'user_pref("media.autoplay.default", 0);\nuser_pref("media.autoplay.block-webaudio", false);\n' > "$prof/user.js"
    setsid firefox --headless --no-remote --profile "$prof" "$(url "$1")" >/dev/null 2>&1 &
    local pid=$!; sleep "$PER_RUN"; kill -- -"$pid" 2>/dev/null; sleep 2; rm -rf "$prof"
}

run_chromium() {
    local prof="$HOME/snap/chromium/common/s7-profile-$1"; rm -rf "$prof"; mkdir -p "$prof"
    setsid chromium --headless=new --user-data-dir="$prof" --autoplay-policy=no-user-gesture-required \
        --no-first-run "$(url "$1")" >/dev/null 2>&1 &
    local pid=$!; sleep "$PER_RUN"; kill -- -"$pid" 2>/dev/null; sleep 2; rm -rf "$prof"
}

for browser in ${BROWSERS:-firefox chromium}; do
    for mode in key nokey wrong; do echo "$browser $mode"; "run_$browser" "$mode"; done
done

kill "$PUB" "$SRV" 2>/dev/null
echo "log: $(ls -t results/s7_2*.jsonl | head -1)"
