#!/usr/bin/env bash
# Repeated minimal-reproducer runs for the upstream VAAPI leak report.
set -u
cd "$(dirname "$0")/.."
DURATION=${DURATION:-600}
OUT=results/repro_$(date +%Y%m%d_%H%M%S).jsonl
mkdir -p results

run() {
    echo "== $*" | tee -a "$OUT.log"
    .venv/bin/python s1_publish/leak_repro.py --duration "$DURATION" --interval 60 --out "$OUT" "$@" \
        2>>"$OUT.log" | grep --line-buffered '"event"' | cut -c1-160
    sleep 3
}

run --encoder vaapi
run --encoder software
run --encoder vaapi --no-simulcast
run --encoder vaapi --no-simulcast --width 640 --height 360
run --encoder vaapi
run --encoder software
echo "results: $OUT"
