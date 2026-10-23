# Solin meeting hub

The hub is a small VPS that gives each hall running Solin hosted meetings two things:
- a public HTTPS address, `https://<hall>.<HUB_DOMAIN>/`, that works even when the hall is behind CGNAT;
- a TURN relay for attendees whose network blocks direct media.

The hub holds **no meeting state**. Lobby, tokens, attendance and media rules all live on the hall PC. One hub can serve many halls.

```
 attendee browser                       hub (this bundle, one VPS)                          hall PC (Solin)
 ───────────────                ─────────────────────────────────────────────          ─────────────────────
 https://hall-x.HUB/ ─TLS:443─► Caddy ─forward_auth─► ask (hall registry,
   GET/POST + WSS (/ws)           │   ◄─ port 200xx ─ TLS gate, frps plugin)
                                  │                      ▲ admin API
                                  └─► 127.0.0.1:200xx ── frps ◄═══ tunnel :7000 ═══ frpc ─► meeting web server
                                                                     (hall dials out)       (aiohttp, plain HTTP:
 media (UDP) ─────────────────► coturn :3478 UDP/TCP, relay ports ◄───────────────────────  join, lobby, /ws → Galene)
   (only when direct fails)       time-limited credentials (secret shared with halls)
```

| Service | Image | Role |
|---|---|---|
| `caddy` | `caddy:2.11.7-alpine` | HTTPS for the hub domain and every hall subdomain. Hall certificates are issued on first visit (on-demand TLS), only for halls that are connected. |
| `frps` | `fatedier/frps:v0.71.1` | Tunnel server. Each hall's `frpc` dials out and publishes **one raw TCP proxy**: its meeting web server. |
| `ask` | built from `ask/` (Python stdlib, `python:3.13.16-alpine3.24`) | Hall registry (`halls.json`), the Caddy TLS gate, Host → tunnel-port routing, the frps server plugin, and the status/directory endpoints. |
| `coturn` | `coturn/coturn:4.18.0` | Dual-stack TURN on UDP and TCP 3478, `use-auth-secret`. Never relays to private or loopback peers. |

All four use host networking. coturn needs a large UDP port range, and everything internal binds to `127.0.0.1`.

## Design choices

**How Caddy reaches a hall: raw TCP proxies, plus a per-request port lookup.**
- Each hall's proxy is `type = "tcp"`. The hub pins it to that hall's port from `halls.json`, on `127.0.0.1:20000-20999`, so it is not reachable from outside.
- For every request to `<hall>.HUB_DOMAIN`, Caddy's `forward_auth` asks the ask service for the hall's port (header `X-Solin-Hall-Port`). It then reverse-proxies to `127.0.0.1:<port>`. This adds a loopback call of under a millisecond per HTTP request. A WebSocket counts as one request.

Why not frp's HTTP vhost (`type = "http"`, `vhostHTTPPort`)?
- In the S5 spike, frp's HTTP vhost proxy closed long-lived WebSockets after about 60 s.
- Raw TCP takes frp out of the HTTP layer entirely. Caddy does all the HTTP and WebSocket handling, and it has no timeout on proxied streams.
- `stream_close_delay 8h` also keeps meetings connected through a `caddy reload`.

Why not `tcpmux`? Caddy cannot speak frp's HTTP CONNECT multiplexer to a plain-HTTP upstream.

What the lookup buys:
- An unknown name gets a 404 page from the hub.
- An offline hall gets a "hall is not connected right now" page (503, `Retry-After`, auto-refresh) instead of a refused connection. S5 showed that a refused port is what makes attendee clients give up.

**Hall authentication: shared frps token + per-hall key (frps server plugin).**
- `FRP_TOKEN` keeps random clients off frps.
- Each hall also sends its name and key in frpc `metadatas`. The ask service implements frps' HTTP plugin protocol:
  - `Login`: accepted only if `sha256(hall_key)` matches the hall's entry in `halls.json`.
  - `NewProxy`: the key is checked again. The proxy must be named exactly after the hall and be `type = "tcp"`. The hub then **rewrites `remote_port`** to the hall's registered port.
- So a hall cannot claim another hall's name, nor pick its own port. frps also rejects a second proxy with the same name.
- `halls.json` holds only key hashes. Edits are picked up live.

**Certificates.** Caddy's `on_demand_tls` asks `/internal/caddy-ask` before issuing a certificate. Only the hub domain and `<registered hall>.HUB_DOMAIN` while that hall's tunnel is online are approved. Random subdomains cannot make the hub burn its Let's Encrypt rate limits.

**TURN.**
- coturn uses `use-auth-secret` with `TURN_SECRET`. Galene on the hall mints time-limited credentials (TURN REST: `username = <expiry>:<name>`, `password = base64(HMAC-SHA1(secret, username))`).
- Relaying to private, loopback, link-local, CGNAT and documentation ranges (IPv4 and IPv6) is denied. Halls and attendees are reached on their public, server-reflexive addresses, so they are unaffected. On 1:1 NAT clouds, the hub's own private address is re-allowed, for relay-to-relay between two allocations.
- `relay-ip` is set explicitly from `PUBLIC_IP` / `PRIVATE_IP` / `PUBLIC_IP6`. Otherwise coturn would also relay from Docker bridge addresses that nobody can reach.

**Lessons from the S5 spike (`spikes/hosted-meetings/RESULTS.md`, "## S5"), applied here:**
1. frp's HTTP vhost proxy closed long-lived WebSockets after about 60 s. This bundle uses `type = "tcp"` proxies only, and the smoke test proves a WebSocket survives 95 s of total silence.
2. `transport.heartbeatTimeout = 90` on frps with `tcpMux` dropped **every tunnel every 90 s**, because frpc sends no app heartbeats when tcpMux is on. It is **not set** in `frps.toml`; do not add it.
3. When a hall's tunnel is down for more than about 25 s, clients exhaust their resume budget. The hub now answers 503 instead of refusing connections, and the attendee page must keep its own rejoin loop.

## Requirements

- A VPS with Debian 12/13 or Ubuntu 22.04/24.04, amd64 or arm64. 1 vCPU / 1 GB RAM is enough for signaling. TURN relaying needs CPU and bandwidth: S5 measured about 0.11 cores for 30 relayed attendees (~16 Mbps). Signaling alone for a 2 h, 50-attendee meeting is about 120–150 MB of egress, so pick a plan with real monthly traffic.
- A public IPv4 address; IPv6 is recommended.
- Firewall / cloud security group, inbound:

  | Port | Protocol | Purpose |
  |---|---|---|
  | 22 | TCP | SSH |
  | 80 | TCP | ACME HTTP challenge, redirect to HTTPS |
  | 443 | TCP (+ UDP for HTTP/3, optional) | HTTPS for the hub and halls |
  | 7000 (`FRP_BIND_PORT`) | TCP | Tunnels from hall PCs |
  | 3478 (`TURN_PORT`) | UDP + TCP | TURN |
  | 49160–49999 (`TURN_MIN_PORT`–`TURN_MAX_PORT`) | UDP | TURN relay ports |

  Nothing else needs to be open. ask (7400), frps admin (7500), Caddy admin (2019) and the hall tunnel ports (20000–20999) listen on 127.0.0.1 only.
- Either a domain, with `HUB_DOMAIN` and `*.HUB_DOMAIN` A/AAAA records pointing to the VPS, **or** no domain at all, using sslip.io (see the next section).

## Without a domain: sslip.io

sslip.io resolves `<anything>.<ip-with-dashes>.sslip.io` to that IP. For a VPS at `203.0.113.10`:
```
HUB_DOMAIN=203-0-113-10.sslip.io
```
- Halls become `https://hall-abc123.203-0-113-10.sslip.io/`, and the hub is `https://203-0-113-10.sslip.io/`.
- Let's Encrypt issues real certificates over the HTTP challenge on port 80. No DNS setup is needed.
- Good for tests and small deployments.
- Caveats:
  - The addresses change if the IP changes.
  - Every sslip.io user may share Let's Encrypt's per-domain rate limits, so issuance can occasionally fail and be retried. Caddy retries on its own, and by default also tries ZeroSSL as a second issuer.
  - Use a real domain for the long-term production hub.
- IPv6 works too: `2001-db8--1.sslip.io`, though halls and attendees mostly use IPv4.

## Setup on a fresh Debian/Ubuntu VPS

```sh
# 1. Docker Engine + compose plugin (official convenience script; or follow docs.docker.com/engine/install)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out/in afterwards

# 2. Firewall (ufw example; also open the same ports in the cloud provider's security group)
sudo apt-get install -y ufw
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw allow 7000/tcp && sudo ufw allow 3478 && sudo ufw allow 49160:49999/udp
sudo ufw enable
# Oracle Cloud Ubuntu images also ship iptables REJECT rules: insert ACCEPTs before them, e.g.
#   sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443,7000,3478 -j ACCEPT
#   sudo iptables -I INPUT 6 -p udp -m multiport --dports 443,3478,49160:49999 -j ACCEPT
#   sudo netfilter-persistent save

# 3. Copy this directory (deploy/hub) to the VPS, e.g. /opt/solin-hub
rsync -a deploy/hub/ vps:/opt/solin-hub/

# 4. Configure
cd /opt/solin-hub
cp hub.env.example hub.env && chmod 600 hub.env
#    edit hub.env: HUB_DOMAIN, ACME_EMAIL, PUBLIC_IP (+ PRIVATE_IP on 1:1-NAT clouds, PUBLIC_IP6),
#    and three fresh secrets: openssl rand -hex 32  (FRP_TOKEN, FRP_ADMIN_PASSWORD, TURN_SECRET)
echo '{"halls": {}}' > halls.json

# 5. Start
docker compose up -d --build
docker compose ps
docker compose logs -f            # Ctrl-C to stop following
curl -s https://$HUB_DOMAIN/      # "The hub is running. Halls connected: 0."
```

Updates: change the image tags in `docker-compose.yml`, then run `docker compose pull && docker compose up -d --build`. Certificates live in the `caddy-data` volume; keep it.

## Registering a hall

```sh
cd /opt/solin-hub
python3 scripts/add_hall.py hall-abc123 --note "Congregation X, contact ..."
# hall:  hall-abc123  (tunnel port 20001)
# key:   <random key, shown once>
```
- This adds `{"hall-abc123": {"key_sha256": ..., "port": 20001}}` to `halls.json` (format: `halls.json.example`).
- The ask service reloads the file automatically.
- Give the hall:
  - its name and key;
  - the hub address (`HUB_DOMAIN`) and `FRP_BIND_PORT`;
  - `FRP_TOKEN` and `TURN_SECRET`.
- Run the same command again to rotate a hall's key: the port is kept. To remove a hall, delete its entry.

Names: lowercase letters, digits and `-`, up to 63 characters. Use something unguessable-ish like `hall-abc123`: the name is the public meeting address.

## How a hall connects

Solin writes `hall/frpc.toml.template` with the values filled in and supervises `frpc -c frpc.toml`:

| Variable | Value |
|---|---|
| `${HUB_ADDR}` | `HUB_DOMAIN` (or the hub's IP) |
| `${HUB_FRP_PORT}` | `FRP_BIND_PORT` (7000) |
| `${FRP_TOKEN}` | `FRP_TOKEN` |
| `${HALL_NAME}` / `${HALL_KEY}` | from `add_hall.py` |
| `${LOCAL_PORT}` | the meeting web server's local port |

Rules the hub enforces:
- exactly one proxy, named `${HALL_NAME}`, `type = "tcp"`;
- no frpc `user`;
- `remotePort` is overwritten by the hub.

`loginFailExit = false` keeps frpc retrying while the hub is unreachable or the key is not registered yet.

Once frpc logs `start proxy success`, the hall is live at `https://${HALL_NAME}.${HUB_DOMAIN}/`. The first visit takes a few seconds while the certificate is issued. The meeting web server receives plain HTTP from `127.0.0.1` with these headers:
- `Host: <hall>.<HUB_DOMAIN>`
- `X-Forwarded-Proto: https`
- `X-Forwarded-Host`
- `X-Forwarded-For: <attendee IP>`

It should build public URLs from `X-Forwarded-Proto` and `Host`.

TURN for Galene's `ice-servers.json` (Solin's `TurnServer`):
- urls: `turn:<HUB_DOMAIN>:3478?transport=udp` and `turn:<HUB_DOMAIN>:3478?transport=tcp`
- secret: `TURN_SECRET`
- credential type: `hmac-sha1`

`turns:` on 5349 is not enabled yet; see "Left for later".

Public endpoints on the hub domain:
- `GET https://HUB_DOMAIN/`: status page (number of connected halls, no names).
- `GET https://HUB_DOMAIN/api/halls/<name>`: `{"online": true|false}`. Solin can use it to confirm its public URL works.
- `GET https://HUB_DOMAIN/healthz`: `ok`.

## Local development variant (`local/`)

Everything runs on one machine:
- `HUB_DOMAIN=hub.localhost`;
- Caddy's internal CA (`local_certs`);
- HTTPS on 8443 and HTTP on 8880;
- test halls `hall-a` / `hall-b` in `local/halls.json`, with keys `hall-a-test-key` / `hall-b-test-key`.

```sh
cd deploy/hub
docker compose --env-file local/hub.env.local up -d --build
docker compose --env-file local/hub.env.local exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > /tmp/hub-ca.crt
# point a hall's frpc at 127.0.0.1:7000 (template above, HALL_NAME=hall-a, HALL_KEY=hall-a-test-key), then:
curl --cacert /tmp/hub-ca.crt https://hall-a.hub.localhost:8443/
docker compose --env-file local/hub.env.local down -v
```
`*.localhost` resolves to 127.0.0.1 in curl and browsers. Browsers need `/tmp/hub-ca.crt` trusted, or a click through the warning.

### Smoke test

```sh
deploy/hub/scripts/smoke_local.sh               # ~2 min; WS_IDLE=10 for a quick run, KEEP=1 to leave it up
```
It brings up the local variant and starts dummy halls (`scripts/hubtest.py hall`: HTTP + WebSocket echo) behind the official `fatedier/frpc:v0.71.1` image. It checks:
- the TLS gate: connected halls are approved; unknown, nested, foreign and offline names are refused;
- HTTPS GET and POST through Caddy → frps → frpc → hall;
- internal endpoints are not reachable from outside;
- a WebSocket stays open through **95 s of silence**;
- hijack attempts fail: a wrong key, and a registered hall publishing another hall's name;
- the offline-hall page;
- a hall reconnects after its frpc restarts;
- TURN allocation over UDP, TCP and IPv6 with time-limited credentials, refusal of expired and forged credentials, and refusal to relay to private or loopback peers;
- then `docker compose down -v`.

`scripts/hubtest.py` also works against a real hub:
```sh
python3 scripts/hubtest.py http https://hall-abc123.$HUB_DOMAIN/
python3 scripts/hubtest.py ws wss://hall-abc123.$HUB_DOMAIN/ws --idle 300   # needs a WS echo at /ws
python3 scripts/hubtest.py turn $PUBLIC_IP --secret "$TURN_SECRET" --peer 8.8.8.8 --denied-peer 10.0.0.1
python3 scripts/hubtest.py turn $PUBLIC_IP --tcp --secret "$TURN_SECRET"
```
Run the TURN check from **outside** the VPS.

## Troubleshooting

| Symptom | Check |
|---|---|
| frpc: `token in login doesn't match` | `FRP_TOKEN` differs between hub.env and the hall. |
| frpc: `unknown hall or wrong hall key` | The hall is not in `halls.json`, or the key is wrong. Check `docker compose logs ask` (`Login rejected`) and the file's validity: ask logs a parse error and keeps the previous registry. |
| frpc: `the proxy must be named ...` / `only one proxy of type 'tcp'` | The hall's frpc.toml is not the template: the proxy name must equal `HALL_NAME`, with no `user`. |
| frpc: `port not allowed` / `port already used` | The hall's `port` in halls.json is outside `HALL_PORT_MIN`–`HALL_PORT_MAX`, or the same hall is connected twice (two Solin instances). |
| Browser: TLS error / `SSL_ERROR_INTERNAL_ERROR_ALERT` on a hall address | No certificate. The hall was not connected the first time someone visited, or ACME failed. Check `docker compose logs caddy` (look for `tls.obtain`, `ask`) and ask's `caddy-ask ... deny`. Ports 80 and 443 must be reachable from the internet for ACME. |
| Hall address shows "not connected right now" | The tunnel is down. Check frpc on the hall and `docker compose logs frps`. |
| WebSockets drop at regular intervals | Make sure nobody added `transport.heartbeatTimeout` to frps.toml, and that halls use `type = "tcp"`. A Caddy reload keeps streams for 8 h; a Caddy restart drops them. |
| TURN does not relay | UDP 3478 and the relay range must be open in **both** the cloud security group and the host firewall. On 1:1 NAT clouds set `PRIVATE_IP`. Check `docker compose logs coturn` for `relay-ip` and `401`/`403`. Remember: peers in private ranges are refused by design. |
| Which halls are online? | `curl -u admin:$FRP_ADMIN_PASSWORD http://127.0.0.1:7500/api/proxy/tcp` on the VPS. |

## Files

```
docker-compose.yml        the four services (host networking, log rotation)
Caddyfile                 production global options (ACME e-mail, on-demand TLS gate) + import
sites.caddy               routing shared by production and local: hub domain, *.hub → hall
frps.toml                 tunnel server (templated from hub.env by frps itself)
turnserver.conf           coturn template; coturn-entrypoint.sh renders it from hub.env
ask/                      control service (ask.py, Dockerfile)
hub.env.example           all settings, documented
halls.json.example        hall registry format
hall/frpc.toml.template   what Solin writes on the hall PC
local/                    local development variant (hub.localhost, internal CA, test halls)
scripts/smoke_local.sh    end-to-end test of the local variant
scripts/hubtest.py        stdlib test client: dummy hall, HTTPS, WebSocket soak, TURN
scripts/add_hall.py       register a hall / rotate its key
```

## Left for later

- **`turns:` on TCP/TLS 5349:** for attendees on networks that only allow TLS. Use Caddy's certificate for `HUB_DOMAIN`, mounted read-only into coturn, plus a reload hook on renewal.
- **Hall self-registration:** an API that issues a name and key. For now an admin runs `add_hall.py`.
- **Per-hall frp tokens:** the shared `FRP_TOKEN` only gates access to frps. The real per-hall authorization is the plugin.
- **Metrics:** coturn and Caddy both have Prometheus endpoints, which are off. frps' dashboard is on 127.0.0.1:7500 (use an SSH tunnel).
- **Still to measure on the real VPS:** the S5 pending items (relay capacity, a 2.5 h soak through Caddy and the TCP tunnel, tunnel-cut recovery) on the production provider.
