#!/usr/bin/env python3
"""Register a hall in halls.json (or rotate its key) and print what the hall needs.

    python3 scripts/add_hall.py hall-abc123 [--halls halls.json] [--note "..."] [--key KEY]

A fresh random key is generated unless --key is given. The hub stores only its sha256; the key
itself is printed once — give it to the hall (Solin: Meeting hosting settings -> hall key).
The ask service picks up the change automatically; no restart needed.

With ``--env hub.env`` (default: ../hub.env when it exists) it also prints the hall's
**connection code** (``solin-hub:<base64url JSON>``), which carries the hub domain,
tunnel address, frp token, the hall's name and key and the TURN secret. Paste it into
Solin: Settings → Integrations → Hosted meetings → Hub connection code.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="hall name = subdomain, e.g. hall-abc123")
    ap.add_argument("--halls", default=os.path.join(here, "..", "halls.json"))
    ap.add_argument("--key", help="use this key instead of generating one")
    ap.add_argument("--note", default="")
    ap.add_argument("--env", default=os.path.join(here, "..", "hub.env"),
                    help="hub.env to read the domain, frp token and TURN secret from")
    ap.add_argument("--port-min", type=int, default=int(os.environ.get("HALL_PORT_MIN", 20000)))
    ap.add_argument("--port-max", type=int, default=int(os.environ.get("HALL_PORT_MAX", 20999)))
    args = ap.parse_args()

    name = args.name.lower()
    if not NAME.match(name):
        print("invalid name: lowercase letters, digits and '-' only", file=sys.stderr)
        return 2
    path = os.path.abspath(args.halls)
    data = {"halls": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    halls = data.setdefault("halls", {})
    entry = halls.get(name)
    if entry is None:
        used = {int(h["port"]) for h in halls.values() if "port" in h}
        free = next((p for p in range(args.port_min + 1, args.port_max + 1) if p not in used), None)
        if free is None:
            print("no free port left in the hall port range", file=sys.stderr)
            return 1
        entry = halls[name] = {"port": free}
    key = args.key or secrets.token_urlsafe(24)
    entry["key_sha256"] = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if args.note:
        entry["note"] = args.note

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".halls.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

    print(f"hall:  {name}  (tunnel port {entry['port']})")
    print(f"key:   {key}")
    print("Give the hall its name and key; the key is not stored on the hub and cannot be shown again.")
    env = read_env(args.env)
    if env.get("HUB_DOMAIN") and env.get("FRP_TOKEN"):
        code = {
            "domain": env["HUB_DOMAIN"], "frp_addr": env.get("HUB_FRP_ADDR") or env["HUB_DOMAIN"],
            "frp_port": int(env.get("FRP_BIND_PORT", "7000")), "frp_token": env["FRP_TOKEN"],
            "hall": name, "hall_key": key,
            "turn_secret": env.get("TURN_SECRET", ""), "turn_port": int(env.get("TURN_PORT", "3478")),
            "turn_host": env.get("TURN_HOST") or env["HUB_DOMAIN"],
        }
        raw = json.dumps(code, separators=(",", ":"), sort_keys=True).encode("utf-8")
        print("connection code (paste into Solin):")
        print("solin-hub:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"))
    return 0


def read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


if __name__ == "__main__":
    sys.exit(main())
