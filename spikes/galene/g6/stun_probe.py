"""Send a STUN binding request and print the reflexive address.

    python3 stun_probe.py <host> [port]
"""

import os
import socket
import struct
import sys

host, port = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3478
family, _, _, _, addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_UDP)[0]
s = socket.socket(family, socket.SOCK_DGRAM)
s.settimeout(3)
tid = os.urandom(12)
s.sendto(struct.pack(">HHI", 0x0001, 0, 0x2112A442) + tid, addr)
data, _ = s.recvfrom(2048)
i = 20
while i < len(data):
    t, n = struct.unpack(">HH", data[i:i + 4])
    v = data[i + 4:i + 4 + n]
    if t == 0x0020:  # XOR-MAPPED-ADDRESS
        fam, xport = struct.unpack(">xBH", v[:4])
        p = xport ^ 0x2112
        if fam == 1:
            ip = socket.inet_ntoa(bytes(a ^ b for a, b in zip(v[4:8], struct.pack(">I", 0x2112A442), strict=True)))
        else:
            ip = socket.inet_ntop(socket.AF_INET6, bytes(a ^ b for a, b in zip(v[4:20], struct.pack(">I", 0x2112A442) + tid, strict=True)))
        print(f"reflexive address: {ip}:{p}")
    i += 4 + n + (-n % 4)
