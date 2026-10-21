"""Caddy on-demand TLS "ask" endpoint: approve a certificate only for the hub domain itself and
for subdomains of halls currently connected to frps (from the frps admin API)."""

import base64
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HUB = os.environ["HUB_DOMAIN"].lower()
AUTH = base64.b64encode(f"admin:{os.environ['FRP_DASH_PASSWORD']}".encode()).decode()


def connected_subdomains() -> set[str]:
    req = urllib.request.Request("http://127.0.0.1:7500/api/proxy/http", headers={"Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=3) as r:
        proxies = json.load(r).get("proxies") or []
    return {p["conf"].get("subdomain", "").lower() for p in proxies if p.get("status") == "online" and p.get("conf")}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        domain = (parse_qs(url.query).get("domain") or [""])[0].lower()
        ok = False
        if url.path == "/caddy-ask":
            if domain == HUB:
                ok = True
            elif domain.endswith("." + HUB):
                try:
                    ok = domain[: -len(HUB) - 1] in connected_subdomains()
                except Exception:  # noqa: BLE001 - deny on any error
                    ok = False
        self.send_response(200 if ok else 403)
        self.end_headers()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 7400), Handler).serve_forever()
