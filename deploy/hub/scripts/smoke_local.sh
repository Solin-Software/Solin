#!/usr/bin/env bash
# End-to-end smoke test of the hub on one machine (local variant: hub.localhost, Caddy internal CA).
#
#   deploy/hub/scripts/smoke_local.sh            # full run (~2 min: includes a 95 s idle WebSocket)
#   WS_IDLE=10 deploy/hub/scripts/smoke_local.sh # quicker
#   KEEP=1 deploy/hub/scripts/smoke_local.sh     # leave the hub running afterwards
#
# Needs: docker with the compose plugin, python3 (standard library only), free ports
# 8880, 8443, 7000, 7400, 7500, 3478 (+ UDP 49160-49260), 18081-18083, 20000-20099.
#
# Brings up Caddy + frps + ask + coturn, starts dummy halls (HTTP + WebSocket echo, like Solin's
# meeting web server) with the official frpc image, and checks:
#   - ask approves certificates for connected halls only; unknown/offline names are refused
#   - HTTPS GET/POST through Caddy -> frps -> frpc -> hall works, with X-Forwarded-* headers
#   - a WebSocket through the hub survives WS_IDLE seconds (default 95) of total silence
#   - a hall with a wrong key cannot claim another hall's name, nor can a registered hall
#     publish under another hall's name; the real hall keeps its address
#   - an offline hall gets the hub's "not connected" page; a restarted frpc gets its address back
#   - TURN allocates with time-limited credentials (UDP, TCP, IPv6), refuses expired/forged
#     ones, and refuses relaying to private/loopback peers
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
ENVFILE=local/hub.env.local
set -a; . "./$ENVFILE"; set +a
WS_IDLE="${WS_IDLE:-95}"
FRPC_IMAGE=fatedier/frpc:v0.71.1
PY="python3 -I $HERE/scripts/hubtest.py"
WORK="$(mktemp -d)"
COMPOSE=(docker compose --env-file "$ENVFILE")
HTTPS="https://%s.${HUB_DOMAIN}:${HTTPS_PORT}"
CONNECT="127.0.0.1:${HTTPS_PORT}"
PIDS=()
FAILS=0

pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
url() { printf "$HTTPS%s" "$1" "${2:-/}"; }

cleanup() {
    set +e
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    docker rm -f smoke-frpc-a smoke-frpc-b smoke-frpc-evil smoke-frpc-steal >/dev/null 2>&1
    if [ "${KEEP:-0}" != 1 ]; then
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    else
        echo "KEEP=1: hub left running (docker compose --env-file $ENVFILE down -v to stop)"
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# frpc config for a hall, rendered from hall/frpc.toml.template exactly as Solin would.
frpc_conf() { # name key local_port [proxy_name_override]
    sed -e "s|\${HUB_ADDR}|127.0.0.1|g" -e "s|\${HUB_FRP_PORT}|${FRP_BIND_PORT}|g" \
        -e "s|\${FRP_TOKEN}|${FRP_TOKEN}|g" -e "s|\${HALL_NAME}|$1|g" -e "s|\${HALL_KEY}|$2|g" \
        -e "s|\${LOCAL_PORT}|$3|g" hall/frpc.toml.template > "$WORK/frpc-$1-$3.toml"
    if [ -n "${4:-}" ]; then sed -i "s|^name = \"$1\"|name = \"$4\"|" "$WORK/frpc-$1-$3.toml"; fi
    echo "$WORK/frpc-$1-$3.toml"
}
start_frpc() { # container conf
    docker run -d --name "$1" --network host -v "$2:/etc/frp/frpc.toml:ro" "$FRPC_IMAGE" -c /etc/frp/frpc.toml >/dev/null
}
start_hall() { # name port
    $PY hall --name "$1" --port "$2" > "$WORK/hall-$1.log" 2>&1 &
    PIDS+=($!)
}
ask_status() { curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${ASK_PORT}$1"; }
online() { curl -s "http://127.0.0.1:${ASK_PORT}/api/halls/$1" | grep -q '"online": true'; }
wait_for() { # seconds description command...
    local t=$1 what=$2; shift 2
    for _ in $(seq "$t"); do "$@" && return 0; sleep 1; done
    echo "timed out waiting for $what" >&2; return 1
}

step "Bring up the local hub"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --quiet-pull 2>&1 | grep -vE '^\s*$' | tail -3
wait_for 30 "ask" bash -c "[ \"\$(curl -s http://127.0.0.1:${ASK_PORT}/healthz)\" = ok ]"
wait_for 30 "caddy" bash -c "curl -sk -o /dev/null -w '%{http_code}' --resolve ${HUB_DOMAIN}:${HTTPS_PORT}:127.0.0.1 https://${HUB_DOMAIN}:${HTTPS_PORT}/healthz | grep -q 200"
"${COMPOSE[@]}" exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$WORK/ca.crt"
CA=(--cacert "$WORK/ca.crt" --connect "$CONNECT")
"${COMPOSE[@]}" ps --format '  {{.Service}}: {{.Status}}'

step "Halls connect (frpc, official image) to dummy meeting web servers"
start_hall hall-a 18081
start_hall hall-b 18082
start_hall evil 18083
start_frpc smoke-frpc-a "$(frpc_conf hall-a hall-a-test-key 18081)"
start_frpc smoke-frpc-b "$(frpc_conf hall-b hall-b-test-key 18082)"
if wait_for 20 "hall-a online" online hall-a && wait_for 20 "hall-b online" online hall-b; then
    pass "hall-a and hall-b connected; ask reports them online"
else
    fail "halls did not come online"; docker logs smoke-frpc-a 2>&1 | tail -5
fi

step "Certificate gate (ask)"
[ "$(ask_status "/internal/caddy-ask?domain=hall-a.${HUB_DOMAIN}")" = 200 ] && pass "connected hall-a approved" || fail "hall-a not approved"
[ "$(ask_status "/internal/caddy-ask?domain=${HUB_DOMAIN}")" = 200 ] && pass "hub domain approved" || fail "hub domain not approved"
[ "$(ask_status "/internal/caddy-ask?domain=nobody.${HUB_DOMAIN}")" = 403 ] && pass "unknown name 'nobody' refused" || fail "unknown name approved"
[ "$(ask_status "/internal/caddy-ask?domain=a.b.${HUB_DOMAIN}")" = 403 ] && pass "nested name refused" || fail "nested name approved"
[ "$(ask_status "/internal/caddy-ask?domain=example.com")" = 403 ] && pass "foreign domain refused" || fail "foreign domain approved"
if $PY http "$(url nobody)" "${CA[@]}" --expect tls-refused >/dev/null; then
    pass "https://nobody.${HUB_DOMAIN}: no certificate issued (TLS refused)"
else
    fail "a certificate was issued for an unknown name"
fi

step "HTTPS through Caddy -> frps -> frpc -> hall"
out="$($PY http "$(url hall-a /join/m1)" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"hall": "hall-a"' && echo "$out" | grep -q '"x-forwarded-proto": "https"' \
    && pass "GET hall-a: $out" || fail "GET hall-a: $out"
out="$($PY http "$(url hall-a /api/lobby)" "${CA[@]}" --post '{"name":"Ana"}' --expect 200 2>&1)" \
    && echo "$out" | grep -q 'Ana' && pass "POST hall-a body echoed: $out" || fail "POST hall-a: $out"
out="$($PY http "$(url hall-b /)" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"hall": "hall-b"' && pass "GET hall-b reaches hall-b" || fail "GET hall-b: $out"
out="$($PY http "https://${HUB_DOMAIN}:${HTTPS_PORT}/api/halls/hall-a" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"online": true' && pass "directory: GET /api/halls/hall-a -> $out" || fail "directory: $out"
out="$($PY http "https://${HUB_DOMAIN}:${HTTPS_PORT}/internal/route" "${CA[@]}" --expect 404 2>&1)" \
    && pass "internal endpoints not reachable from outside" || fail "internal endpoint exposed: $out"

step "Long-lived WebSocket (${WS_IDLE} s idle) through the hub, in the background"
$PY ws "$(url hall-a /ws)" "${CA[@]}" --idle "$WS_IDLE" > "$WORK/ws.log" 2>&1 &
WS_PID=$!
sleep 2
head -1 "$WORK/ws.log"

step "Hijack attempts on hall-a's name"
start_frpc smoke-frpc-evil "$(frpc_conf hall-a wrong-key 18083)"
start_frpc smoke-frpc-steal "$(frpc_conf hall-b hall-b-test-key 18083 hall-a)"
sleep 4
docker logs smoke-frpc-evil 2>&1 | grep -q "wrong hall key" \
    && pass "frpc claiming hall-a with a wrong key: login refused" || { fail "wrong-key frpc not refused"; docker logs smoke-frpc-evil 2>&1 | tail -3; }
docker logs smoke-frpc-steal 2>&1 | grep -q "must be named 'hall-b'" \
    && pass "hall-b (valid key) publishing a proxy named hall-a: refused" || { fail "proxy theft not refused"; docker logs smoke-frpc-steal 2>&1 | tail -3; }
out="$($PY http "$(url hall-a /after-hijack)" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"hall": "hall-a"' && pass "hall-a still served by the real hall" || fail "hall-a after hijack: $out"
docker rm -f smoke-frpc-evil smoke-frpc-steal >/dev/null

step "Offline hall"
docker rm -f smoke-frpc-b >/dev/null
wait_for 10 "hall-b offline" bash -c "! curl -s http://127.0.0.1:${ASK_PORT}/api/halls/hall-b | grep -q true" || true
out="$($PY http "$(url hall-b /)" "${CA[@]}" --expect 503 2>&1)" && echo "$out" | grep -q "not connected" \
    && pass "hall-b offline: hub answers 503 'not connected' page" || fail "offline hall-b: $out"
[ "$(ask_status "/internal/caddy-ask?domain=hall-b.${HUB_DOMAIN}")" = 403 ] \
    && pass "offline hall-b: new certificates refused" || fail "offline hall-b still approved"

step "TURN (coturn) with time-limited credentials"
if $PY turn 127.0.0.1 --port "$TURN_PORT" --secret "$TURN_SECRET" --peer 8.8.8.8 \
        --denied-peer 10.1.2.3 --denied-peer 192.168.1.1 --denied-peer 172.16.0.1 --denied-peer 127.0.0.1 \
        --denied-peer 100.64.0.1 --denied-peer 169.254.169.254; then :; else fail "TURN over UDP"; fi
if $PY turn 127.0.0.1 --tcp --port "$TURN_PORT" --secret "$TURN_SECRET" --peer 1.1.1.1 --denied-peer 10.0.0.1; then :
else fail "TURN over TCP"; fi
if python3 -c 'import socket; s=socket.socket(socket.AF_INET6, socket.SOCK_DGRAM); s.connect(("::1", 9))' 2>/dev/null; then
    if $PY turn ::1 --port "$TURN_PORT" --relay-family 6 --secret "$TURN_SECRET" \
            --peer 2606:4700:4700::1111 --denied-peer fd00::1 --denied-peer fe80::1 --denied-peer ::1; then :
    else fail "TURN over IPv6"; fi
else
    echo "(no IPv6 loopback here: IPv6 TURN check skipped)"
fi

step "Waiting for the WebSocket soak to finish"
if wait "$WS_PID"; then pass "$(tail -1 "$WORK/ws.log" | sed 's/^PASS: //')"; else fail "WebSocket: $(cat "$WORK/ws.log")"; fi

step "Hall reconnects after its tunnel drops"
docker restart smoke-frpc-a >/dev/null
if wait_for 20 "hall-a back online" online hall-a \
        && out="$($PY http "$(url hall-a /back)" "${CA[@]}" --expect 200 2>&1)"; then
    pass "hall-a back at the same address after an frpc restart"
else
    fail "hall-a did not come back: ${out:-}"
fi

step "Result"
if [ "$FAILS" -eq 0 ]; then
    printf '\033[32mALL CHECKS PASSED\033[0m\n'
else
    printf '\033[31m%d CHECK(S) FAILED\033[0m\n' "$FAILS"
    "${COMPOSE[@]}" logs --no-color --tail 30 ask frps caddy 2>&1 | tail -60
    exit 1
fi
