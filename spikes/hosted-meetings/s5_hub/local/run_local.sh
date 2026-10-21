#!/usr/bin/env bash
# Bring up hub + hall locally: hub (Caddy :8880, frps, ask, coturn), hall LiveKit with hub TURN, frpc.
set -eu
cd "$(dirname "$0")"
set -a; . ./hub.env.local; set +a
export HUB_ENV_FILE="$PWD/hub.env.local" LOCAL_DIR="$PWD"
docker compose -p s5hub -f ../hub/docker-compose.yml -f compose.local.yml --env-file hub.env.local up -d
sed -e "s/__TURN_HOST__/$PRIVATE_IP/g" -e "s/__TURN_SECRET__/$TURN_SECRET/g" ../hall/livekit-hall.yaml > /tmp/livekit-hall.local.yaml
docker rm -f lk-s2 lk-s5-hall >/dev/null 2>&1 || true
docker run -d --name lk-s5-hall --network host -v /tmp/livekit-hall.local.yaml:/livekit.yaml:ro \
    livekit/livekit-server --config /livekit.yaml >/dev/null
docker rm -f s5-frpc >/dev/null 2>&1 || true
docker run -d --name s5-frpc --network host -e HUB_ADDR=127.0.0.1 -e FRP_TOKEN="$FRP_TOKEN" -e HALL_NAME=hall-a \
    -v "$PWD/../hall/frpc.toml:/etc/frp/frpc.toml:ro" fatedier/frpc:v0.71.1 -c /etc/frp/frpc.toml >/dev/null
sleep 4
docker ps --filter name=s5 --filter name=lk-s5 --format '{{.Names}}: {{.Status}}'
