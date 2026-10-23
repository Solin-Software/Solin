"""Galene's on-disk configuration for one hall.

Layout under ``root``::

    data/config.json        allowOrigin (the meeting web server's origins)
    data/ice-servers.json   STUN/TURN from the hub (absent = host candidates only)
    groups/<slug>.json      one group per meeting: tokens only, no recording

Galene re-reads ``config.json`` and group files when they change; ``ice-servers.json``
is cached for 2-5 minutes, so it is written before Galene starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .tokens import group_jwk

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


@dataclass(frozen=True, slots=True)
class TurnServer:
    """The hub's TURN relay; Galene mints time-limited credentials from ``secret``."""

    urls: tuple[str, ...]
    secret: str
    username: str = "solin"

    def stun_urls(self) -> tuple[str, ...]:
        hosts: list[str] = []
        for url in self.urls:
            if url.startswith("turn:"):
                host = url[len("turn:"):].split("?", 1)[0]
                stun = f"stun:{host}"
                if stun not in hosts:
                    hosts.append(stun)
        return tuple(hosts)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class GaleneLayout:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.data = self.root / "data"
        self.groups = self.root / "groups"
        self.static = self.root / "static"

    def prepare(self) -> None:
        for directory in (self.data, self.groups, self.static):
            directory.mkdir(parents=True, exist_ok=True)
        # Galene refuses to start without a static directory; Solin serves every
        # page itself, so a placeholder index is enough.
        index = self.static / "index.html"
        if not index.exists():
            index.write_text("<!doctype html><title>Solin</title>", encoding="utf-8")

    def write_server_config(self, allowed_origins: Iterable[str]) -> None:
        origins = sorted({origin for origin in allowed_origins if origin})
        _write_json(self.data / "config.json", {"allowOrigin": origins} if origins else {})

    def write_ice_servers(self, turn: TurnServer | None) -> None:
        path = self.data / "ice-servers.json"
        if turn is None:
            path.unlink(missing_ok=True)
            return
        servers: list[dict[str, object]] = []
        stun = turn.stun_urls()
        if stun:
            servers.append({"urls": list(stun)})
        servers.append({
            "urls": list(turn.urls),
            "username": turn.username,
            "credential": turn.secret,
            "credentialType": "hmac-sha1",
        })
        _write_json(path, servers)

    def write_group(self, slug: str, key: bytes, *, display_name: str = "", max_clients: int = 300) -> Path:
        if not _SLUG.match(slug):
            raise ValueError(f"invalid group name {slug!r}")
        path = self.groups / f"{slug}.json"
        group: dict[str, object] = {
            "codecs": ["h264", "opus"],
            "allow-recording": False,
            "max-clients": int(max_clients),
            "max-history-age": 60,
            "authKeys": [group_jwk(key)],
        }
        if display_name:
            group["displayName"] = display_name
        _write_json(path, group)
        return path

    def remove_group(self, slug: str) -> None:
        (self.groups / f"{slug}.json").unlink(missing_ok=True)

    def clear_groups(self) -> None:
        if self.groups.exists():
            shutil.rmtree(self.groups)
        self.groups.mkdir(parents=True, exist_ok=True)
