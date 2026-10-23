#!/usr/bin/env bash
# G5: S3 on Galene. Remote commenter (headless Chromium) -> Galene -> hall speakers -> hall feed
# (emulated echo path) -> program -> Galene -> remote; with and without echo cancellation.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
G=$DIR/..
RUN=$G/run
RES=$G/results
PY=${PY:-/home/jonata/Projetos/Solin/spikes/hosted-meetings/.venv/bin/python}
CDP_PY=${CDP_PY:-/home/jonata/Projetos/Solin/.venv/bin/python}
GALENE=${GALENE:-/home/jonata/Projetos/upstream/galene/galene}
MIC=${MIC:-alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback}
PHASE=${PHASE:-90}
TS=$(date +%Y%m%d_%H%M%S)
HALL=$RES/g5_hall_${TS}.jsonl
mkdir -p "$RES"

(cd "$RUN" && echo '{"allowOrigin": ["http://127.0.0.1:8091"]}' > data/config.json && rm -f data/ice-servers.json &&
 "$GALENE" -insecure -http 127.0.0.1:8443 -turn "" -data data -groups groups -static static > logs/g5_galene.out 2>&1 &)
(cd "$G/g1" && LOG_NAME=g5_remote WEB=../g5/web python3 log_server.py > "$RUN/logs/g5_log.out" 2>&1 &)
sleep 2

phase() {
    local label=$1; shift
    echo "== $label"
    "$PY" "$DIR/hall_galene.py" --group g5 --mic "$MIC" ${PLAY:+--play-target "$PLAY"} --play-gain "${GAIN:-1}" \
        ${ECHO:+--echo-delay-ms "$ECHO" --echo-gain "${ECHO_GAIN:-0.5}"} --duration $((PHASE + 15)) --out "$HALL" "$@" \
        >/dev/null 2>>"$RUN/logs/g5_hall.log" &
    local hall=$!
    sleep 6
    local prof="$HOME/snap/chromium/common/g5-profile"; rm -rf "$prof"; mkdir -p "$prof"
    chromium --headless=new --user-data-dir="$prof" --autoplay-policy=no-user-gesture-required \
        --remote-debugging-port=9333 --no-first-run "http://127.0.0.1:8091/g5.html?group=g5&label=$label" \
        >/dev/null 2>&1 &
    sleep "$PHASE"
    "$CDP_PY" /home/jonata/Projetos/Solin/spikes/hosted-meetings/s3_audio/cdp_close.py 9333
    wait "$hall"
    rm -rf "$prof"
}

phase aec-off
phase aec-on --aec
phase aec-ns-on --aec --ns
pkill -x galene
for p in $(pgrep -f '^python3 log_server.py'); do kill "$p"; done
REMOTE=$(ls -t "$RES"/g5_remote_*.jsonl | head -1)
echo "hall: $HALL  remote: $REMOTE"
python3 "$DIR/analyze.py" "$HALL" "$REMOTE"
