#!/bin/bash
# A/B the whippub sender-report clock (pts vs send time) under the same rig,
# then re-measure the capture offset in libobs. Needs Galene on :8443 and the
# probe page open in Chromium with --remote-debugging-port=9334.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-/home/jonata/Projetos/Solin/spikes/hosted-meetings/.venv/bin/python}
CDP_PY=${CDP_PY:-/home/jonata/Projetos/Solin/.venv/bin/python}
MIC=${MIC:-alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback}
SECONDS_PER_RUN=${SECONDS_PER_RUN:-300}
RESULTS=$DIR/../results
cd "$DIR"
for mode in "$@"; do
    LOG_NAME=g1_sr_$mode python3 log_server.py &
    LOG=$!
    sleep 1
    taskset -c 0-7 "$PY" publish_d.py --webcam /dev/video0 --mic "$MIC" --sr "$mode" \
        --duration "$SECONDS_PER_RUN" --out "$RESULTS/g1_sr_${mode}_stats.jsonl" &
    PUB=$!
    sleep 4
    "$CDP_PY" cdp_eval.py 9334 "location.reload(); 'ok'" >/dev/null
    wait $PUB
    "$CDP_PY" cdp_eval.py 9334 "document.getElementById('panel').textContent" 
    kill $LOG
    sleep 2
done
taskset -c 0-7 "$PY" raw_check.py --webcam /dev/video0 --mic "$MIC" --seconds 40 2>&1 | grep -E 'pairs|libobs timestamps'
