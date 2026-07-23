from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, ip_address
from typing import Final


PRIVATE_LAN_NETWORKS: Final = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)


def parse_private_lan_ipv4(value: str | IPv4Address) -> IPv4Address:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise ValueError("A valid private LAN IPv4 address is required") from error
    if not isinstance(address, IPv4Address) or not any(
        address in network for network in PRIVATE_LAN_NETWORKS
    ):
        raise ValueError("A private RFC 1918 IPv4 address is required")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError("The address must identify a private LAN interface")
    return address


__all__ = ["PRIVATE_LAN_NETWORKS", "parse_private_lan_ipv4"]
