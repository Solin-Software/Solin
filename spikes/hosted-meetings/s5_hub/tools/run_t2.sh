#!/usr/bin/env bash
# S5 test 2 (signaling cost via the hub), detached-safe: publisher + VM sampler + 50 attendees.
set -u
cd "$(dirname "$0")/../.."
DUR=${DUR:-300}
TAG=${TAG:-t2b}
.venv/bin/python s1_publish/publish_b.py --url ws://127.0.0.1:7880 --room s5 --codec h264 --encoder software \
    --duration $((DUR + 120)) --interval 120 >/dev/null 2>results/s5gcp_${TAG}_pub.log &
sleep 8
ssh -o BatchMode=yes solin-hub 'awk "/ens4:/{print \$10}" /proc/net/dev' > results/s5gcp_${TAG}_tx_before.txt
ssh -o BatchMode=yes solin-hub "python3 ~/tools/vm_sample.py --iface ens4 --interval 30 --duration $((DUR + 60))" \
    > results/s5gcp_${TAG}_vm.jsonl &
.venv/bin/python s5_hub/tools/relay_load.py --direct --url ws://35.247.228.99 --room s5 --identity-prefix hub \
    --n 50 --per-second 5 --quality medium --duration "$DUR" --interval 60 --out results/s5gcp_${TAG}.jsonl >/dev/null 2>&1
wait
ssh -o BatchMode=yes solin-hub 'awk "/ens4:/{print \$10}" /proc/net/dev' > results/s5gcp_${TAG}_tx_after.txt
echo done > results/s5gcp_${TAG}.done
