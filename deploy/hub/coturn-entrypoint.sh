#!/bin/sh
# Render turnserver.conf from the environment (hub.env) and start coturn.
set -eu
: "${TURN_SECRET:?TURN_SECRET must be set in hub.env}"
: "${HUB_DOMAIN:?HUB_DOMAIN must be set in hub.env}"
TURN_PORT="${TURN_PORT:-3478}"
TURN_MIN_PORT="${TURN_MIN_PORT:-49160}"
TURN_MAX_PORT="${TURN_MAX_PORT:-49999}"
TURN_TOTAL_QUOTA="${TURN_TOTAL_QUOTA:-1000}"
out=/tmp/turnserver.conf
esc() { printf '%s' "$1" | sed 's/[\/&|]/\\&/g'; }
sed -e "s|\${TURN_PORT}|$(esc "$TURN_PORT")|g" \
    -e "s|\${TURN_MIN_PORT}|$(esc "$TURN_MIN_PORT")|g" \
    -e "s|\${TURN_MAX_PORT}|$(esc "$TURN_MAX_PORT")|g" \
    -e "s|\${TURN_SECRET}|$(esc "$TURN_SECRET")|g" \
    -e "s|\${HUB_DOMAIN}|$(esc "$HUB_DOMAIN")|g" \
    -e "s|\${TURN_TOTAL_QUOTA}|$(esc "$TURN_TOTAL_QUOTA")|g" \
    /etc/coturn/turnserver.conf.template > "$out"
# Relay addresses. Without relay-ip lines coturn relays from EVERY local address, including
# Docker bridges (172.17.0.1...) that peers cannot reach, so production sets them explicitly:
#  - 1:1 NAT clouds (Google Cloud, Oracle, AWS): the VM only sees PRIVATE_IP; advertise PUBLIC_IP.
#  - VPS with the public IPv4 on its interface: relay from PUBLIC_IP directly.
#  - PUBLIC_IP6 (optional): IPv6 relay address (no NAT on IPv6).
# All empty (local development): coturn discovers the addresses itself.
if [ -n "${PRIVATE_IP:-}" ] && [ -n "${PUBLIC_IP:-}" ] && [ "$PRIVATE_IP" != "$PUBLIC_IP" ]; then
    echo "relay-ip=${PRIVATE_IP}" >> "$out"
    echo "external-ip=${PUBLIC_IP}/${PRIVATE_IP}" >> "$out"
    # Relay-to-relay (two allocations on this hub) reaches the hub's own private address.
    echo "allowed-peer-ip=${PRIVATE_IP}" >> "$out"
elif [ -n "${PUBLIC_IP:-}" ]; then
    echo "relay-ip=${PUBLIC_IP}" >> "$out"
fi
if [ -n "${PUBLIC_IP6:-}" ]; then
    echo "relay-ip=${PUBLIC_IP6}" >> "$out"
fi
# Extra raw lines (one per ';'), e.g. for local testing: TURN_EXTRA="allowed-peer-ip=192.168.1.5"
if [ -n "${TURN_EXTRA:-}" ]; then
    printf '%s\n' "$TURN_EXTRA" | tr ';' '\n' >> "$out"
fi
exec turnserver -c "$out"
