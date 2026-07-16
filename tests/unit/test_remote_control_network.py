from __future__ import annotations

from ipaddress import IPv4Address

from solin.core.remote_control.network import LanInterface, resolve_selected_interface


def test_selection_key_binds_interface_and_address() -> None:
    interface = LanInterface("ethernet", "Ethernet", IPv4Address("192.168.1.10"))

    assert interface.selection_key == "ethernet|192.168.1.10"


def test_persisted_interface_selection_fails_closed_when_address_changes() -> None:
    current = LanInterface("ethernet", "Ethernet", IPv4Address("192.168.1.11"))

    assert resolve_selected_interface("ethernet|192.168.1.10", [current]) is None


def test_persisted_interface_selection_resolves_exact_live_interface() -> None:
    expected = LanInterface("wifi", "Wi-Fi", IPv4Address("10.0.0.8"))
    other = LanInterface("ethernet", "Ethernet", IPv4Address("192.168.1.4"))

    assert resolve_selected_interface(expected.selection_key, [other, expected]) == expected
