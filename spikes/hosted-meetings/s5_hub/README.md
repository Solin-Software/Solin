# S5 hub on Oracle Cloud

Hub = Caddy (on-demand TLS per hall subdomain) → frps (reverse tunnel to hall PCs) + coturn (TURN relay).
Validated locally with `local/run_local.sh` (see RESULTS.md, S5).

## 1. What you need to do (accounts and DNS)

1. **Oracle Cloud account** (Always Free). Region: the one closest to the congregations.
2. **VM**: Compute → Instances → Create
   - Image: Ubuntu 24.04 (aarch64)
   - Shape: `VM.Standard.A1.Flex`, 2 OCPU / 12 GB (the current Always Free limit)
   - Add your SSH public key
   - Reserve a public IP (Networking → IP management) and attach it, so it survives reboots
3. **Ingress rules** on the VCN security list (or an NSG) for the VM's subnet:
   | Port | Protocol | Purpose |
   |---|---|---|
   | 22 | TCP | SSH |
   | 80, 443 | TCP | Caddy (ACME + HTTPS) |
   | 7000 | TCP | frp tunnel from hall PCs |
   | 3478 | UDP + TCP | TURN |
   | 50000–50999 | UDP | TURN relay ports |
4. **DNS** for a (sub)domain you control, e.g. `hub.example.org`:
   - `hub.example.org` A → reserved public IP
   - `*.hub.example.org` A → reserved public IP
5. Tell me the SSH host (e.g. an entry in your `~/.ssh/config`) — I'll do the rest from here using your existing SSH key.

## 2. What I'll do on the VM

```
# Oracle's Ubuntu images block ports in iptables even when the security list allows them
sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443,7000,3478 -j ACCEPT
sudo iptables -I INPUT 6 -p udp -m multiport --dports 3478,50000:50999 -j ACCEPT
sudo netfilter-persistent save
# Docker + compose plugin, then:
cp hub.env.example hub.env   # HUB_DOMAIN, PUBLIC_IP, PRIVATE_IP (ip -4 addr), fresh secrets
docker compose --env-file hub.env up -d
```

Hall side (this PC): `hall/frpc.toml` (dials out to the hub) and `hall/livekit-hall.yaml` (TURN = hub).

## 3. Measurements

| Test | Tool | What it answers |
|---|---|---|
| Relay capacity | `tools/relay_load.py` (N attendees forced through TURN, no decoding) from 1–2 load machines/VMs + `tools/vm_sample.py` on the hub | CPU and Mbps per relayed attendee on 2 OCPU; where it saturates |
| Signaling soak | `tools/signal_soak.py --hours 2.5` through `wss://<hall>.hub…` | Does the tunnel + WebSocket survive a full meeting |
| Tunnel recovery | drop the hall's network for 10–60 s during the soak | How frpc/LiveKit reconnect; what attendees see |
| Idle reclamation | VM CPU/network history over a week | Does Oracle flag the hub as idle |

The load generator must not run on the hub VM itself, and a single laptop process tops out well
before the hub does (decoding/stats overhead) — run it from 1–2 separate machines.
