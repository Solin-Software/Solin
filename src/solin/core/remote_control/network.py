from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address

from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

from .certificates import parse_private_lan_ipv4


@dataclass(frozen=True, slots=True)
class LanInterface:
    """One eligible, explicitly bindable LAN IPv4 interface."""

    identifier: str
    display_name: str
    ipv4_address: IPv4Address

    @property
    def selection_key(self) -> str:
        return f"{self.identifier}|{self.ipv4_address}"


def discover_lan_interfaces(
    interfaces_provider: Callable[[], Iterable[QNetworkInterface]] | None = None,
) -> tuple[LanInterface, ...]:
    provider = interfaces_provider or QNetworkInterface.allInterfaces
    candidates: dict[str, LanInterface] = {}
    for interface in provider():
        flags = interface.flags()
        if not flags & QNetworkInterface.InterfaceFlag.IsUp:
            continue
        if not flags & QNetworkInterface.InterfaceFlag.IsRunning:
            continue
        if flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
            continue

        identifier = (interface.name() or str(interface.index())).strip()
        display_name = (interface.humanReadableName() or identifier).strip()
        for entry in interface.addressEntries():
            address = entry.ip()
            if address.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            try:
                ipv4_address = parse_private_lan_ipv4(address.toString())
            except ValueError:
                continue
            candidate = LanInterface(identifier, display_name, ipv4_address)
            candidates[candidate.selection_key] = candidate

    return tuple(
        sorted(
            candidates.values(),
            key=lambda item: (item.display_name.casefold(), int(item.ipv4_address)),
        )
    )


def resolve_selected_interface(
    selection_key: str,
    available: Iterable[LanInterface],
) -> LanInterface | None:
    """Resolve a persisted selection against live interfaces, failing closed."""

    normalized = str(selection_key or "").strip()
    if not normalized:
        return None
    return next(
        (item for item in available if item.selection_key == normalized),
        None,
    )
