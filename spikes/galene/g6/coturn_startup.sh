#!/bin/bash
# G6 dual-stack TURN relay (temporary spike VM).
# Reference copy of the G6 VM startup script. The spike substituted the secret
# before uploading; set TURN_SECRET in the script (or the VM environment) to reuse it.
apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q coturn
systemctl stop coturn; systemctl disable coturn
V4INT=$(curl -s -H Metadata-Flavor:Google http://metadata/computeMetadata/v1/instance/network-interfaces/0/ip)
V4EXT=$(curl -s -H Metadata-Flavor:Google http://metadata/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
V6=$(ip -6 addr show scope global | awk '/inet6/{print $2}' | cut -d/ -f1 | head -1)
cat > /etc/turnserver-g6.conf <<CONF
listening-port=3478
listening-ip=$V4INT
listening-ip=$V6
relay-ip=$V4INT
relay-ip=$V6
external-ip=$V4EXT/$V4INT
min-port=49160
max-port=49260
use-auth-secret
static-auth-secret=${TURN_SECRET:?set TURN_SECRET}
realm=solin-g6
fingerprint
no-tls
no-dtls
no-cli
log-file=/var/log/turn-g6.log
simple-log
verbose
CONF
nohup turnserver -c /etc/turnserver-g6.conf > /var/log/turn-g6.stdout 2>&1 &
