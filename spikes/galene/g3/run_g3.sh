#!/bin/bash
# G3: hall capacity. Galene + path D publisher + loadgen on one machine (loopback, as S2).
# Cores: publisher 0-7, Galene 8-15, loadgen 16-23. Writes results/g3_<variant>.{jsonl,pidstat,pub}.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
G=$DIR/..
RUN=$G/run
RES=$G/results
PY=${PY:-/home/jonata/Projetos/Solin/spikes/hosted-meetings/.venv/bin/python}
CLIP=${CLIP:-/home/jonata/Projetos/Solin/spikes/hosted-meetings/s1_publish/media/testclip.mp4}
GALENE=${GALENE:-/home/jonata/Projetos/upstream/galene/galene}
SECS=${SECS:-120}
mkdir -p "$RES"

(cd "$RUN" && echo '{}' > data/config.json && rm -f data/ice-servers.json &&
 taskset -c 8-15 "$GALENE" -insecure -http 127.0.0.1:8443 -turn "" -data data -groups groups -static static \
   > logs/g3_galene.out 2>&1 &)
sleep 2
GPID=$(pgrep -x galene)

publisher() {   # $1 = width, $2 = height, $3 = kbps
    (cd "$G/g1" && taskset -c 0-7 "$PY" publish_d.py --clip "$CLIP" --url http://127.0.0.1:8443/group/g3/.whip \
        --width "$1" --height "$2" --bitrate "$3" --interval 10 > "$RES/g3_pub_$1.out" 2>&1 &)
    sleep 10
}
stop_publisher() {
    for p in $(pgrep -f "^$PY publish_d.py"); do kill -INT "$p"; done
    sleep 5
}

variant() {     # $1 = name, $2 = viewers, $3.. = loadgen flags
    local name=$1 n=$2; shift 2
    local pub=$(pgrep -f "^$PY publish_d.py" | head -1) wp=$(pgrep -x whippub | head -1)
    echo "== $name ($n viewers)"
    pidstat -u -r -h -p "$GPID,$pub,$wp" 10 > "$RES/g3_$name.pidstat" 2>&1 &
    local ps=$!
    taskset -c 16-23 "$DIR/loadgen/loadgen" -group g3 -n "$n" -duration "${SECS}s" "$@" > "$RES/g3_$name.jsonl"
    kill $ps
    sleep 3
}

publisher 1280 720 1700
variant audio_50 50 -audio-only
variant 720p_50 50
variant 720p_100 100
stop_publisher
publisher 640 360 450
variant 360p_50 50
variant 360p_100 100
stop_publisher
kill "$GPID"
echo done
