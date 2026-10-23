#!/usr/bin/env python3
"""Standard-library test helpers for the Solin hub (used by smoke_local.sh, usable on a real hub).

  hubtest.py hall --port 18081 --name hall-a
      Dummy hall: a tiny HTTP server (GET -> JSON echo of path + forwarded headers, POST -> echo
      of the body) with a WebSocket echo at /ws, like Solin's meeting web server.
  hubtest.py http URL [--cacert CA] [--connect HOST:PORT] [--post DATA] [--expect STATUS]
      One HTTPS request; prints status + body.
  hubtest.py ws URL [--cacert CA] [--connect HOST:PORT] [--idle 95]
      WebSocket through the hub: echo, stay completely idle for --idle seconds, echo again.
  hubtest.py turn HOST [--port 3478] --secret S [--tcp] [--peer IP] [--denied-peer IP]
      TURN allocation with time-limited (TURN REST) credentials; also checks that expired and
      wrongly signed credentials are refused, and optionally CreatePermission results.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------- WebSocket framing
def ws_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        head += key
    return head + payload


async def ws_read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    key = await reader.readexactly(4) if b1 & 0x80 else None
    data = await reader.readexactly(n)
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data


# --------------------------------------------------------------------------- dummy hall
async def hall_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            method, target, _ = line.decode("latin-1").split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, v = h.decode("latin-1").split(":", 1)
                headers[k.strip().lower()] = v.strip()
            if headers.get("upgrade", "").lower() == "websocket" and target.startswith("/ws"):
                accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode()).digest())
                writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                await writer.drain()
                print(f"[hall {name}] websocket open", flush=True)
                while True:
                    op, data = await ws_read(reader)
                    if op == 8:
                        writer.write(ws_frame(8, data[:2], False))
                        await writer.drain()
                        print(f"[hall {name}] websocket closed by client", flush=True)
                        return
                    if op == 9:
                        writer.write(ws_frame(10, data, False))
                    elif op in (1, 2):
                        writer.write(ws_frame(op, data, False))
                    await writer.drain()
            length = int(headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            if method == "POST":
                out = json.dumps({"hall": name, "echo": body.decode("utf-8", "replace")}).encode()
            else:
                out = json.dumps({"hall": name, "path": target, "host": headers.get("host"),
                                  "x-forwarded-proto": headers.get("x-forwarded-proto"),
                                  "x-forwarded-host": headers.get("x-forwarded-host"),
                                  "x-forwarded-for": headers.get("x-forwarded-for")}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(out)).encode() + b"\r\n\r\n" + out)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def run_hall(args: argparse.Namespace) -> int:
    server = await asyncio.start_server(lambda r, w: hall_conn(r, w, args.name), "127.0.0.1", args.port)
    print(f"[hall {args.name}] listening on 127.0.0.1:{args.port}", flush=True)
    async with server:
        await server.serve_forever()
    return 0


# --------------------------------------------------------------------------- HTTPS client side
def tls_context(cacert: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


async def open_conn(url: str, cacert: str | None, connect: str | None):
    u = urlparse(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme in ("https", "wss") else 80)
    chost, cport = (connect.rsplit(":", 1) if connect else (host, port))
    secure = u.scheme in ("https", "wss")
    reader, writer = await asyncio.open_connection(
        chost, int(cport), ssl=tls_context(cacert) if secure else None, server_hostname=host if secure else None)
    hosthdr = host if u.port is None else f"{host}:{u.port}"
    return reader, writer, hosthdr, (u.path or "/") + (f"?{u.query}" if u.query else "")


async def run_http(args: argparse.Namespace) -> int:
    try:
        reader, writer, host, path = await open_conn(args.url, args.cacert, args.connect)
    except ssl.SSLError as e:
        print(f"TLS refused ({e.reason})")
        if args.expect != "tls-refused":
            print("FAIL: TLS handshake failed", file=sys.stderr)
            return 1
        return 0
    if args.expect == "tls-refused":
        print("FAIL: expected the TLS handshake to be refused (no certificate)", file=sys.stderr)
        return 1
    body = args.post.encode() if args.post is not None else b""
    method = "POST" if args.post is not None else "GET"
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    if b"transfer-encoding: chunked" in head.lower():
        out, rest = b"", payload
        while rest:
            size_s, _, rest = rest.partition(b"\r\n")
            size = int(size_s, 16)
            if size == 0:
                break
            out, rest = out + rest[:size], rest[size + 2:]
        payload = out
    print(f"{status} {payload.decode('utf-8', 'replace').strip()[:400]}")
    if args.expect and status != int(args.expect):
        print(f"FAIL: expected HTTP {args.expect}", file=sys.stderr)
        return 1
    return 0


async def run_ws(args: argparse.Namespace) -> int:
    reader, writer, host, path = await open_conn(args.url, args.cacert, args.connect)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    await writer.drain()
    status = await reader.readline()
    while (await reader.readline()) not in (b"\r\n", b""):
        pass
    if b" 101 " not in status:
        print(f"FAIL: handshake answered {status!r}", file=sys.stderr)
        return 1

    async def echo(msg: str) -> None:
        writer.write(ws_frame(1, msg.encode(), True))
        await writer.drain()
        op, data = await asyncio.wait_for(ws_read(reader), 10)
        if op != 1 or data.decode() != msg:
            raise RuntimeError(f"bad echo: op={op} data={data!r}")

    t0 = time.monotonic()
    await echo("hello")
    print(f"websocket open, echo ok; now idle for {args.idle:.0f} s (no frames at all)", flush=True)
    try:
        op, data = await asyncio.wait_for(ws_read(reader), args.idle)
        print(f"FAIL: unexpected frame/close while idle after {time.monotonic() - t0:.1f} s: op={op} {data!r}",
              file=sys.stderr)
        return 1
    except asyncio.TimeoutError:
        pass
    except (asyncio.IncompleteReadError, ConnectionError) as e:
        print(f"FAIL: connection dropped while idle after {time.monotonic() - t0:.1f} s: {e!r}", file=sys.stderr)
        return 1
    await echo("still-here")
    print(f"PASS: websocket alive after {time.monotonic() - t0:.1f} s, echo ok", flush=True)
    writer.write(ws_frame(8, struct.pack("!H", 1000), True))
    await writer.drain()
    writer.close()
    return 0


# --------------------------------------------------------------------------- TURN client
MAGIC = 0x2112A442
ATTR_USERNAME, ATTR_MI, ATTR_ERROR, ATTR_REALM, ATTR_NONCE = 0x0006, 0x0008, 0x0009, 0x0014, 0x0015
ATTR_XOR_PEER, ATTR_XOR_RELAYED, ATTR_REQ_TRANSPORT, ATTR_LIFETIME = 0x0012, 0x0016, 0x0019, 0x000D
ATTR_REQ_FAMILY = 0x0017


def stun_attr(t: int, v: bytes) -> bytes:
    return struct.pack("!HH", t, len(v)) + v + b"\x00" * ((4 - len(v) % 4) % 4)


def xor_addr(ip: str, port: int, txid: bytes) -> bytes:
    a = ipaddress.ip_address(ip)
    xport = port ^ (MAGIC >> 16)
    if a.version == 4:
        return struct.pack("!BBH", 0, 1, xport) + (int(a) ^ MAGIC).to_bytes(4, "big")
    mask = int.from_bytes(struct.pack("!I", MAGIC) + txid, "big")
    return struct.pack("!BBH", 0, 2, xport) + (int(a) ^ mask).to_bytes(16, "big")


def parse_xor_addr(v: bytes, txid: bytes) -> str:
    fam, xport = v[1], struct.unpack("!H", v[2:4])[0] ^ (MAGIC >> 16)
    if fam == 1:
        ip = ipaddress.IPv4Address(int.from_bytes(v[4:8], "big") ^ MAGIC)
        return f"{ip}:{xport}"
    mask = int.from_bytes(struct.pack("!I", MAGIC) + txid, "big")
    return f"[{ipaddress.IPv6Address(int.from_bytes(v[4:20], 'big') ^ mask)}]:{xport}"


class Turn:
    def __init__(self, host: str, port: int, tcp: bool) -> None:
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.socket(fam, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM)
        self.sock.settimeout(5)
        self.sock.connect((host, port))
        self.tcp = tcp
        self.realm = self.nonce = b""
        self.key = b""
        self.user = b""

    def _recv(self) -> bytes:
        if not self.tcp:
            return self.sock.recv(4096)
        head = b""
        while len(head) < 20:
            head += self.sock.recv(20 - len(head))
        n = struct.unpack("!H", head[2:4])[0]
        body = b""
        while len(body) < n:
            body += self.sock.recv(n - len(body))
        return head + body

    def request(self, mtype: int, attrs: bytes, auth: bool,
                txid: bytes | None = None) -> tuple[int, dict[int, bytes], bytes]:
        txid = txid or os.urandom(12)
        if auth:
            attrs += stun_attr(ATTR_USERNAME, self.user) + stun_attr(ATTR_REALM, self.realm) \
                + stun_attr(ATTR_NONCE, self.nonce)
            head = struct.pack("!HHI", mtype, len(attrs) + 24, MAGIC) + txid
            attrs += stun_attr(ATTR_MI, hmac.new(self.key, head + attrs, hashlib.sha1).digest())
        self.sock.send(struct.pack("!HHI", mtype, len(attrs), MAGIC) + txid + attrs)
        resp = self._recv()
        rtype, rlen = struct.unpack("!HH", resp[:4])
        out: dict[int, bytes] = {}
        i = 20
        while i < 20 + rlen:
            t, n = struct.unpack("!HH", resp[i:i + 4])
            out[t] = resp[i + 4:i + 4 + n]
            i += 4 + n + (4 - n % 4) % 4
        return rtype, out, txid

    @staticmethod
    def error(attrs: dict[int, bytes]) -> int:
        e = attrs.get(ATTR_ERROR, b"\x00\x00\x00\x00")
        return e[2] * 100 + e[3]

    def allocate(self, user: str, password: str, family: int = 4) -> tuple[bool, str]:
        transport = stun_attr(ATTR_REQ_TRANSPORT, bytes([17, 0, 0, 0]))
        if family == 6:  # RFC 6156: ask for an IPv6 relayed address
            transport += stun_attr(ATTR_REQ_FAMILY, bytes([2, 0, 0, 0]))
        rtype, attrs, _ = self.request(0x0003, transport, auth=False)
        if rtype != 0x0113 or self.error(attrs) != 401:
            return False, f"unexpected first answer type={rtype:#x} err={self.error(attrs)}"
        self.realm, self.nonce, self.user = attrs[ATTR_REALM], attrs[ATTR_NONCE], user.encode()
        self.key = hashlib.md5(self.user + b":" + self.realm + b":" + password.encode()).digest()
        rtype, attrs, txid = self.request(0x0003, transport, auth=True)
        if rtype == 0x0103:
            return True, parse_xor_addr(attrs[ATTR_XOR_RELAYED], txid)
        return False, f"error {self.error(attrs)}"

    def permission(self, peer: str) -> tuple[bool, int]:
        # XOR-PEER-ADDRESS is xored with the transaction id for IPv6, so pick the id first.
        txid = os.urandom(12)
        rtype, attrs, _ = self.request(0x0008, stun_attr(ATTR_XOR_PEER, xor_addr(peer, 9, txid)), True, txid)
        return rtype == 0x0108, self.error(attrs)

    def release(self) -> None:
        try:
            self.request(0x0004, stun_attr(ATTR_LIFETIME, struct.pack("!I", 0)), auth=True)
        except OSError:
            pass
        self.sock.close()


def rest_credentials(secret: str, ttl: int, name: str = "smoke") -> tuple[str, str]:
    user = f"{int(time.time()) + ttl}:{name}"
    pw = base64.b64encode(hmac.new(secret.encode(), user.encode(), hashlib.sha1).digest()).decode()
    return user, pw


def run_turn(args: argparse.Namespace) -> int:
    proto = "TCP" if args.tcp else "UDP"
    where = f"{args.host}:{args.port}/{proto}"
    fails = 0

    t = Turn(args.host, args.port, args.tcp)
    ok, info = t.allocate(*rest_credentials(args.secret, 3600), family=args.relay_family)
    print(f"{'PASS' if ok else 'FAIL'}: allocate (IPv{args.relay_family} relay) with valid time-limited "
          f"credentials on {where}: {info}")
    fails += not ok
    if ok:
        for peer in args.peer or []:
            good, err = t.permission(peer)
            print(f"{'PASS' if good else 'FAIL'}: permission to public peer {peer} granted"
                  + ("" if good else f" (error {err})"))
            fails += not good
        for peer in args.denied_peer or []:
            good, err = t.permission(peer)
            print(f"{'PASS' if not good else 'FAIL'}: permission to private peer {peer} refused"
                  + (f" (error {err})" if not good else " -- it was GRANTED"))
            fails += good
        t.release()

    for label, creds in (("expired credentials", rest_credentials(args.secret, -60)),
                         ("credentials signed with a wrong secret", rest_credentials(args.secret + "x", 3600))):
        t = Turn(args.host, args.port, args.tcp)
        ok, info = t.allocate(*creds)
        print(f"{'PASS' if not ok else 'FAIL'}: {label} refused on {where} ({info})")
        fails += ok
        if ok:
            t.release()
        else:
            t.sock.close()
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("hall")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--name", default="hall")
    for name in ("http", "ws"):
        p = sub.add_parser(name)
        p.add_argument("url")
        p.add_argument("--cacert")
        p.add_argument("--connect", help="HOST:PORT to connect to instead of resolving the URL host")
        if name == "http":
            p.add_argument("--post")
            p.add_argument("--expect", help="HTTP status, or 'tls-refused'")
        else:
            p.add_argument("--idle", type=float, default=95)
    p = sub.add_parser("turn")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=3478)
    p.add_argument("--secret", required=True)
    p.add_argument("--tcp", action="store_true")
    p.add_argument("--relay-family", type=int, choices=(4, 6), default=4)
    p.add_argument("--peer", action="append", help="public peer that must be permitted")
    p.add_argument("--denied-peer", action="append", help="private peer that must be refused")
    args = ap.parse_args()
    if args.cmd == "turn":
        return run_turn(args)
    return asyncio.run({"hall": run_hall, "http": run_http, "ws": run_ws}[args.cmd](args))


if __name__ == "__main__":
    sys.exit(main())
