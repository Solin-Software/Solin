"""Solin hub control service ("ask"). Standard library only; holds no meeting state.

Internal endpoints (bound to 127.0.0.1, never routed publicly by Caddy):
  GET  /internal/caddy-ask?domain=<host>  Caddy on-demand TLS gate: 200 for the hub domain and
                                          for <hall>.<hub> while that hall's tunnel is online.
  GET  /internal/route                    Caddy forward_auth: maps the request's Host to the
                                          hall's tunnel port (X-Solin-Hall-Port), or answers
                                          the visitor directly when the hall is offline/unknown.
  POST /internal/frps-plugin?op=...       frps server plugin (Login, NewProxy): only registered
                                          halls with the right key may connect, each hall may
                                          only publish its own proxy, and the hub decides the
                                          proxy's remote port.
Public endpoints (served on https://<HUB_DOMAIN>/):
  GET  /                    tiny status page
  GET  /api/halls/<name>    {"online": bool}
  GET  /healthz             "ok"

Halls are registered in halls.json (re-read automatically when the file changes):
  {"halls": {"hall-abc123": {"key_sha256": "<sha256 hex of the hall key>", "port": 20001}}}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("ask")

HUB = os.environ["HUB_DOMAIN"].strip().lower().rstrip(".")
LISTEN = os.environ.get("ASK_ADDR", "127.0.0.1")
PORT = int(os.environ.get("ASK_PORT", "7400"))
HALLS_FILE = os.environ.get("HALLS_FILE", "/hub/halls.json")
FRP_ADMIN = f"http://127.0.0.1:{os.environ.get('FRP_ADMIN_PORT', '7500')}"
FRP_AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['FRP_ADMIN_PASSWORD']}".encode()).decode()
PORT_MIN = int(os.environ.get("HALL_PORT_MIN", "20000"))
PORT_MAX = int(os.environ.get("HALL_PORT_MAX", "20999"))
ONLINE_CACHE_S = 2.0

HALL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PORT_HEADER = "X-Solin-Hall-Port"


class Halls:
    """halls.json, reloaded when its mtime/size changes. Invalid entries are skipped (logged)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._stamp: tuple[float, int] | None = None
        self._halls: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> dict | None:
        self._maybe_reload()
        return self._halls.get(name)

    def _maybe_reload(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            if self._stamp is not None:
                log.warning("halls file %s disappeared; no hall can connect", self.path)
            self._stamp, self._halls = None, {}
            return
        stamp = (st.st_mtime, st.st_size)
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp == self._stamp:
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f).get("halls") or {}
            except (OSError, ValueError, AttributeError) as e:
                log.error("cannot read %s (%s); keeping the previous registry", self.path, e)
                self._stamp = stamp
                return
            halls: dict[str, dict] = {}
            ports: dict[int, str] = {}
            for name, entry in raw.items():
                name = str(name).lower()
                try:
                    digest = str(entry["key_sha256"]).lower()
                    port = int(entry["port"])
                except (KeyError, TypeError, ValueError):
                    log.error("hall %r: needs key_sha256 and port; skipped", name)
                    continue
                if not HALL_NAME.match(name):
                    log.error("hall %r: invalid name (lowercase letters, digits, '-'); skipped", name)
                elif not re.fullmatch(r"[0-9a-f]{64}", digest):
                    log.error("hall %r: key_sha256 must be 64 hex chars; skipped", name)
                elif not PORT_MIN <= port <= PORT_MAX:
                    log.error("hall %r: port %d outside %d-%d; skipped", name, port, PORT_MIN, PORT_MAX)
                elif port in ports:
                    log.error("hall %r: port %d already used by %r; skipped", name, port, ports[port])
                else:
                    ports[port] = name
                    halls[name] = {"key_sha256": digest, "port": port}
            self._halls, self._stamp = halls, stamp
            log.info("loaded %d hall(s) from %s", len(halls), self.path)

    def check_key(self, name: str, key: str) -> dict | None:
        hall = self.get(name)
        if hall is None or not key:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return hall if hmac.compare_digest(digest, hall["key_sha256"]) else None


class Online:
    """Names of tcp proxies frps reports online (admin API), cached briefly."""

    def __init__(self) -> None:
        self._at = 0.0
        self._names: set[str] = set()
        self._lock = threading.Lock()

    def names(self) -> set[str]:
        with self._lock:
            if time.monotonic() - self._at > ONLINE_CACHE_S:
                req = urllib.request.Request(FRP_ADMIN + "/api/proxy/tcp", headers={"Authorization": FRP_AUTH})
                with urllib.request.urlopen(req, timeout=3) as r:
                    proxies = json.load(r).get("proxies") or []
                self._names = {p.get("name", "") for p in proxies if p.get("status") == "online"}
                self._at = time.monotonic()
            return set(self._names)

    def is_online(self, name: str) -> bool:
        try:
            return name in self.names()
        except Exception as e:  # noqa: BLE001 - frps unreachable: treat as offline
            log.warning("frps admin API unavailable: %s", e)
            return False


halls = Halls(HALLS_FILE)
online = Online()


def hall_from_host(host: str) -> str | None:
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("["):
        return None
    host = host.split(":", 1)[0]
    suffix = "." + HUB
    if not host.endswith(suffix):
        return None
    name = host[: -len(suffix)]
    return name if HALL_NAME.match(name) else None


def plugin_decision(op: str, body: dict) -> dict:
    content = body.get("content") or {}
    if op == "Login":
        metas = content.get("metas") or {}
        name = str(metas.get("hall", "")).lower()
        if halls.check_key(name, str(metas.get("hall_key", ""))) is None:
            log.info("Login rejected: hall=%r from %s", name, content.get("client_address"))
            return {"reject": True, "reject_reason": "unknown hall or wrong hall key"}
        log.info("Login ok: hall=%r from %s", name, content.get("client_address"))
        return {"reject": False, "unchange": True}
    if op == "NewProxy":
        metas = (content.get("user") or {}).get("metas") or {}
        name = str(metas.get("hall", "")).lower()
        hall = halls.check_key(name, str(metas.get("hall_key", "")))
        if hall is None:
            return {"reject": True, "reject_reason": "unknown hall or wrong hall key"}
        if content.get("proxy_name") != name:
            log.info("NewProxy rejected: hall=%r tried proxy %r", name, content.get("proxy_name"))
            return {"reject": True, "reject_reason": f"the proxy must be named {name!r} (and frpc 'user' left empty)"}
        if content.get("proxy_type") != "tcp":
            return {"reject": True, "reject_reason": "only one proxy of type 'tcp' is allowed"}
        content["remote_port"] = hall["port"]  # the hub, not the hall, decides the port
        log.info("NewProxy ok: hall=%r -> 127.0.0.1:%d", name, hall["port"])
        return {"reject": False, "unchange": False, "content": content}
    return {"reject": False, "unchange": True}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}h1{{font-size:1.3rem}}</style>
</head><body><h1>{title}</h1><p>{text}</p></body></html>"""


def page(title: str, text: str, refresh: int | None = None) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return PAGE.format(refresh=meta, title=html.escape(title), text=text).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "solin-hub-ask"
    sys_version = ""

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        if path == "/internal/caddy-ask":
            domain = (parse_qs(url.query).get("domain") or [""])[0].lower().rstrip(".")
            name = hall_from_host(domain)
            ok = domain == HUB or (name is not None and halls.get(name) is not None and online.is_online(name))
            log.info("caddy-ask %s -> %s", domain, "allow" if ok else "deny")
            self._send(200 if ok else 403)
        elif path == "/internal/route":
            self._route()
        elif path == "/healthz":
            self._send(200, b"ok\n")
        elif path == "/":
            try:
                n = len([x for x in online.names() if halls.get(x) is not None])
                text = f"The hub is running. Halls connected: {n}."
            except Exception:  # noqa: BLE001
                text = "The hub is running, but the tunnel server is not answering."
            self._send(200, page("Solin meeting hub", text), "text/html; charset=utf-8")
        elif path.startswith("/api/halls/"):
            name = path[len("/api/halls/"):].strip("/").lower()
            if not HALL_NAME.match(name):
                self._json(400, {"error": "invalid hall name"})
            else:
                self._json(200, {"online": halls.get(name) is not None and online.is_online(name)})
        else:
            self._send(404, b"not found\n")

    def _route(self) -> None:
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        name = hall_from_host(host)
        hall = halls.get(name) if name else None
        if hall is None:
            self._send(404, page("No such meeting address",
                                 "Check the link you received from your congregation."),
                       "text/html; charset=utf-8")
        elif not online.is_online(name):
            self._send(503, page("The hall is not connected right now",
                                 "The meeting may not have started yet, or the hall's internet connection "
                                 "dropped for a moment. This page retries automatically.", refresh=10),
                       "text/html; charset=utf-8", {"Retry-After": "5"})
        else:
            self._send(200, headers={PORT_HEADER: str(hall["port"])})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/internal/frps-plugin":
            self._send(404, b"not found\n")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            op = (parse_qs(url.query).get("op") or [body.get("op", "")])[0]
            self._json(200, plugin_decision(op, body))
        except Exception as e:  # noqa: BLE001 - fail closed
            log.exception("plugin error")
            self._json(200, {"reject": True, "reject_reason": f"hub plugin error: {e}"})

    def log_message(self, fmt: str, *args) -> None:  # access log is noisy (forward_auth per request)
        pass


def main() -> None:
    logging.basicConfig(level=os.environ.get("ASK_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    halls.get("")  # load + validate at startup so errors show in the logs
    server = ThreadingHTTPServer((LISTEN, PORT), Handler)
    server.daemon_threads = True
    log.info("ask listening on %s:%d, hub domain %s, halls file %s", LISTEN, PORT, HUB, HALLS_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()
