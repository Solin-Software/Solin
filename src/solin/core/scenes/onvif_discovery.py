"""ONVIF camera discovery: a WS-Discovery probe plus a device query.

The probe finds ONVIF cameras on the local networks by multicasting to the
WS-Discovery group from every IPv4 interface. The query then asks one camera,
over its device service, for what Solin needs to add it: the RTSP address of
each media profile, and the PTZ service with the profiles that can move.

Budget firmware is the norm here, so both halves tolerate the usual quirks:
cameras that advertise an address other than the one they answered from, and
replies with unescaped ``&`` inside RTSP addresses.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import re
import select
import socket
import time
from typing import Protocol
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid
from xml.etree import ElementTree

from solin.core.scenes.ptz import (
    PtzCancellation,
    PtzCancelledError,
    PtzCredentials,
    PtzExecutionError,
)
from solin.core.scenes.ptz_onvif import (
    OnvifHttpResponse,
    OnvifTransport,
    _soap_envelope,
)


WSD_MULTICAST_GROUP = ("239.255.255.250", 3702)
WSD_PORT = 3702
DEFAULT_DISCOVERY_SECONDS = 3.0
DEFAULT_ADDRESS_SEARCH_SECONDS = 2.5
MAXIMUM_SEARCH_ADDRESSES = 1024
# Where ONVIF device services usually listen, for a camera that ignores probes:
# the HTTP default, Xiongmai, TP-Link Tapo, and two common vendor ports.
ONVIF_FALLBACK_PORTS = (80, 8899, 2020, 8000, 8080)
_FALLBACK_CALL_SECONDS = 2.0
DEFAULT_CALL_SECONDS = 8.0

_DEVICE_NAMESPACE = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NAMESPACE = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NAMESPACE = "http://www.onvif.org/ver10/schema"
_ACTION_GET_SYSTEM_DATE_AND_TIME = f"{_DEVICE_NAMESPACE}/GetSystemDateAndTime"
_ACTION_GET_CAPABILITIES = f"{_DEVICE_NAMESPACE}/GetCapabilities"
_ACTION_GET_PROFILES = f"{_MEDIA_NAMESPACE}/GetProfiles"
_ACTION_GET_STREAM_URI = f"{_MEDIA_NAMESPACE}/GetStreamUri"

_PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope'
    ' xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<s:Header>"
    '<a:Action s:mustUnderstand="1">'
    "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
    "<a:MessageID>uuid:{message_id}</a:MessageID>"
    "<a:ReplyTo><a:Address>"
    "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
    "</a:Address></a:ReplyTo>"
    '<a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
    "</s:Header>"
    "<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>"
    "</s:Envelope>"
)
_MAXIMUM_DATAGRAM_BYTES = 65535
_MAXIMUM_REPLIES = 512
_PROBE_REPEAT_SECONDS = 1.0
_AFTER_LAST_SEND_SECONDS = 1.5
_MAXIMUM_PROBE_SECONDS = 15.0
_HOSTS_PER_SOCKET = 64
_MAXIMUM_SWEEP_SOCKETS = 16
_POLL_SECONDS = 0.1
_MAXIMUM_PROFILES = 32
# An '&' that does not start an entity: Xiongmai-style firmwares write
# '&channel=1&stream=0' into RTSP addresses without escaping it.
_BARE_AMPERSAND = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")
_SCOPE_PREFIX = "onvif://www.onvif.org/"


class OnvifDiscoveryError(RuntimeError):
    """A discovery or query failure, named by a stable error code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class OnvifDiscoveredCamera:
    key: str
    device_service: str
    host: str
    port: int
    name: str = ""
    hardware: str = ""
    location: str = ""

    @property
    def display_name(self) -> str:
        if self.name and self.hardware and self.hardware.casefold() not in self.name.casefold():
            return f"{self.name} {self.hardware}"
        return self.name or self.hardware or self.host


@dataclass(frozen=True, slots=True)
class OnvifMediaProfile:
    token: str
    name: str
    stream_uri: str
    ptz: bool


@dataclass(frozen=True, slots=True)
class OnvifDeviceDetails:
    device_service: str
    ptz_endpoint: str
    profiles: tuple[OnvifMediaProfile, ...]

    @property
    def ptz_profiles(self) -> tuple[OnvifMediaProfile, ...]:
        return tuple(profile for profile in self.profiles if profile.ptz)


class WsDiscoveryTransport(Protocol):
    def probe(
        self,
        message: bytes,
        *,
        timeout_seconds: float,
        cancellation: PtzCancellation,
        hosts: Sequence[str] = (),
    ) -> Sequence[tuple[bytes, str]]:
        """Send ``message`` and return every ``(payload, sender_ip)`` reply.

        With no ``hosts`` the probe is multicast to the local networks;
        otherwise it goes straight to each host, which also reaches cameras
        behind a router.
        """
        ...


def multicast_ipv4_addresses() -> tuple[str, ...]:
    """The IPv4 address of every interface that is up and can multicast."""
    from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

    flags = QNetworkInterface.InterfaceFlag
    addresses: list[str] = []
    for interface in QNetworkInterface.allInterfaces():
        state = interface.flags()
        if not state & flags.IsUp or not state & flags.IsRunning:
            continue
        if state & flags.IsLoopBack or not state & flags.CanMulticast:
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                addresses.append(address.toString())
    return tuple(dict.fromkeys(addresses))


class UdpWsDiscoveryTransport:
    """One UDP socket that sends the probe out of each interface in turn.

    Replies come back unicast to the socket's port, so a single socket bound to
    every address collects them all. The probe goes out twice: it is one UDP
    datagram, and a camera that misses it would simply not be listed.
    """

    def __init__(
        self,
        interface_addresses: Callable[[], Iterable[str]] = multicast_ipv4_addresses,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interface_addresses = interface_addresses
        self._clock = clock

    def probe(
        self,
        message: bytes,
        *,
        timeout_seconds: float,
        cancellation: PtzCancellation,
        hosts: Sequence[str] = (),
    ) -> Sequence[tuple[bytes, str]]:
        addresses: tuple[str, ...] = ()
        if not hosts:
            try:
                addresses = tuple(self._interface_addresses())
            except Exception:  # noqa: BLE001 - interface listing is best effort
                addresses = ()
        # A sweep sends to hosts that may not exist. Each such datagram waits on
        # ARP, holding send-buffer space until ARP gives up about three seconds
        # later, and one socket's buffer holds only about a hundred of them.
        # Several sockets keep a whole sweep in flight at once.
        count = min(_MAXIMUM_SWEEP_SOCKETS, max(1, -(-len(hosts) // _HOSTS_PER_SOCKET)))
        replies: list[tuple[bytes, str]] = []
        with ExitStack() as stack:
            sockets = [stack.enter_context(self._open_socket()) for _ in range(count)]
            pending: deque[str] = deque(hosts)
            sent_any = False
            if not hosts:
                self._send(sockets[0], message, addresses)
            started = self._clock()
            last_send = started
            # Re-sending a single datagram covers its loss; a sweep is too
            # large to send twice.
            repeated = len(hosts) > 1
            while len(replies) < _MAXIMUM_REPLIES:
                if cancellation.cancelled:
                    raise PtzCancelledError
                now = self._clock()
                for sock in sockets:
                    while pending:
                        try:
                            sock.sendto(message, (pending[0], WSD_PORT))
                        except (BlockingIOError, InterruptedError):
                            break  # this socket is full; ARP will drain it
                        except OSError:
                            pass  # an unroutable or unresolvable host is skipped
                        else:
                            sent_any = True
                            last_send = now
                        pending.popleft()
                if hosts and not pending and not sent_any:
                    raise OnvifDiscoveryError("onvif_discovery_unavailable")
                deadline = started + _MAXIMUM_PROBE_SECONDS
                if not pending:
                    deadline = min(
                        deadline,
                        max(started + timeout_seconds, last_send + _AFTER_LAST_SEND_SECONDS),
                    )
                if now >= deadline:
                    break
                if not repeated and now - started >= _PROBE_REPEAT_SECONDS:
                    repeated = True
                    if hosts:
                        pending.extend(hosts)
                    else:
                        self._send(sockets[0], message, addresses, required=False)
                readable, _, _ = select.select(
                    sockets, sockets if pending else [], [], min(_POLL_SECONDS, deadline - now),
                )
                for sock in readable:
                    try:
                        payload, sender = sock.recvfrom(_MAXIMUM_DATAGRAM_BYTES)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        # Windows reports an ICMP "port unreachable" from an
                        # earlier send as an error on the next receive.
                        continue
                    replies.append((payload, str(sender[0])))
        return replies

    @staticmethod
    def _open_socket() -> socket.socket:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        except OSError as error:
            raise OnvifDiscoveryError("onvif_discovery_unavailable") from error
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.bind(("", 0))
            sock.setblocking(False)
        except OSError as error:
            sock.close()
            raise OnvifDiscoveryError("onvif_discovery_unavailable") from error
        return sock

    @staticmethod
    def _send(
        sock: socket.socket,
        message: bytes,
        addresses: Sequence[str],
        *,
        required: bool = True,
    ) -> None:
        sent = False
        for address in addresses:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(address),
                )
                sock.sendto(message, WSD_MULTICAST_GROUP)
                sent = True
            except OSError:
                continue
        if sent:
            return
        # No usable interface was listed: let the routing table pick one.
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton("0.0.0.0"),
            )
            sock.sendto(message, WSD_MULTICAST_GROUP)
        except OSError as error:
            if required:
                raise OnvifDiscoveryError("onvif_discovery_unavailable") from error


def build_probe_message(message_id: str) -> bytes:
    return _PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")


def parse_probe_matches(payload: bytes, sender: str) -> tuple[OnvifDiscoveredCamera, ...]:
    """Cameras described by one ProbeMatches reply; malformed replies yield none."""
    root = _parse_xml(payload)
    if root is None:
        return ()
    cameras: list[OnvifDiscoveredCamera] = []
    for match in root.findall(".//{*}ProbeMatch"):
        xaddrs = (_child_text(match, "XAddrs") or "").split()
        device_service = _reachable_xaddr(xaddrs, sender)
        if not device_service:
            continue
        parsed = urlsplit(device_service)
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            continue
        host = parsed.hostname or ""
        if not host:
            continue
        reference = match.find("{*}EndpointReference/{*}Address")
        key = (reference.text or "").strip() if reference is not None else ""
        scopes = _scope_values((_child_text(match, "Scopes") or "").split())
        cameras.append(OnvifDiscoveredCamera(
            key=key or device_service,
            device_service=device_service,
            host=host,
            port=port,
            name=scopes.get("name", ""),
            hardware=scopes.get("hardware", ""),
            location=scopes.get("location", ""),
        ))
    return tuple(cameras)


def discover_onvif_cameras(
    transport: WsDiscoveryTransport,
    *,
    timeout_seconds: float = DEFAULT_DISCOVERY_SECONDS,
    cancellation: PtzCancellation | None = None,
    message_id: str | None = None,
    hosts: Sequence[str] = (),
) -> tuple[OnvifDiscoveredCamera, ...]:
    message = build_probe_message(message_id or str(uuid.uuid4()))
    replies = transport.probe(
        message,
        timeout_seconds=timeout_seconds,
        cancellation=cancellation or PtzCancellation(),
        hosts=hosts,
    )
    cameras: dict[str, OnvifDiscoveredCamera] = {}
    for payload, sender in replies:
        for camera in parse_probe_matches(payload, sender):
            cameras.setdefault(camera.key, camera)
    return tuple(sorted(cameras.values(), key=_camera_sort_key))


def parse_search_target(text: str) -> tuple[str, ...]:
    """The hosts named by an address, a hostname, a range, or a network.

    Accepted forms: ``192.168.0.100``, ``camera.local``, ``192.168.0.10-50``,
    ``192.168.0.10-192.168.0.50``, and ``192.168.0.0/24``. A target that
    names more than :data:`MAXIMUM_SEARCH_ADDRESSES` hosts is refused.
    """
    target = text.strip()
    if not target or any(character.isspace() for character in target):
        raise OnvifDiscoveryError("onvif_search_target_invalid")
    try:
        if "/" in target:
            network = ipaddress.IPv4Network(target, strict=False)
            if network.num_addresses > MAXIMUM_SEARCH_ADDRESSES + 2:
                raise OnvifDiscoveryError("onvif_search_target_too_large")
            hosts = list(network.hosts()) or [network.network_address]
            return tuple(str(host) for host in hosts)
        if "-" in target and _looks_like_ipv4_range(target):
            first_text, _, last_text = target.partition("-")
            first = ipaddress.IPv4Address(first_text)
            if "." in last_text:
                last = ipaddress.IPv4Address(last_text)
            else:
                last = ipaddress.IPv4Address(
                    (int(first) & 0xFFFFFF00) | _octet(last_text)
                )
            if last < first:
                raise OnvifDiscoveryError("onvif_search_target_invalid")
            if int(last) - int(first) + 1 > MAXIMUM_SEARCH_ADDRESSES:
                raise OnvifDiscoveryError("onvif_search_target_too_large")
            return tuple(
                str(ipaddress.IPv4Address(value))
                for value in range(int(first), int(last) + 1)
            )
        return (str(ipaddress.ip_address(target)),)
    except ValueError:
        pass
    if _HOSTNAME.fullmatch(target) and not re.fullmatch(r"[0-9.-]+", target):
        return (target,)
    raise OnvifDiscoveryError("onvif_search_target_invalid")


def search_onvif_address(
    target: str,
    *,
    discovery_transport: WsDiscoveryTransport,
    onvif_transport: OnvifTransport,
    cancellation: PtzCancellation | None = None,
    timeout_seconds: float = DEFAULT_ADDRESS_SEARCH_SECONDS,
) -> tuple[OnvifDiscoveredCamera, ...]:
    """Probe the hosts of ``target`` directly.

    A single host that ignores WS-Discovery is still found when one of the
    usual ONVIF ports answers a device-service request.
    """
    hosts = parse_search_target(target)
    cancellation = cancellation or PtzCancellation()
    cameras = discover_onvif_cameras(
        discovery_transport,
        timeout_seconds=timeout_seconds,
        cancellation=cancellation,
        hosts=hosts,
    )
    if cameras or len(hosts) != 1:
        return cameras
    camera = _probe_device_service_ports(hosts[0], onvif_transport, cancellation)
    return (camera,) if camera is not None else ()


def device_service_url(host: str, port: int = 80) -> str:
    """The conventional device-service address for a camera known only by host."""
    host = host.strip()
    if not host:
        raise OnvifDiscoveryError("onvif_address_invalid")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port == 80 else f"{host}:{port}"
    return f"http://{netloc}/onvif/device_service"


def query_onvif_device(
    device_service: str,
    credentials: PtzCredentials | None,
    *,
    transport: OnvifTransport,
    cancellation: PtzCancellation | None = None,
    call_timeout_seconds: float = DEFAULT_CALL_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OnvifDeviceDetails:
    """Ask one camera for its RTSP streams and PTZ service.

    The sequence is the one ONVIF Device Manager uses: the camera's clock first
    (unauthenticated, so the WS-Security timestamps that follow match it), then
    the service addresses, the media profiles, and a stream address per profile.
    """
    parsed = urlsplit(device_service)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OnvifDiscoveryError("onvif_address_invalid")
    host = parsed.hostname
    cancellation = cancellation or PtzCancellation()

    def call(
        endpoint: str,
        action: str,
        operation: ElementTree.Element,
        *,
        authenticated: bool,
        clock_offset_seconds: float = 0.0,
    ) -> ElementTree.Element:
        login = credentials if authenticated else None
        try:
            response = transport.call(
                endpoint=endpoint,
                action=action,
                payload=_soap_envelope(
                    operation,
                    login,
                    clock_offset_seconds=clock_offset_seconds,
                ),
                credentials=login,
                timeout_seconds=call_timeout_seconds,
                cancellation=cancellation,
            )
        except PtzCancelledError:
            raise
        except (TimeoutError, OSError) as error:
            raise OnvifDiscoveryError("onvif_unreachable") from error
        except PtzExecutionError as error:
            if error.error_code.startswith("ptz_tls"):
                raise OnvifDiscoveryError("onvif_tls_failed") from error
            raise OnvifDiscoveryError("onvif_unreachable") from error
        return _checked_root(response)

    offset = 0.0
    try:
        clock = call(
            device_service,
            _ACTION_GET_SYSTEM_DATE_AND_TIME,
            _device_operation("GetSystemDateAndTime"),
            authenticated=False,
        )
    except OnvifDiscoveryError as error:
        # Some cameras refuse even this anonymously; only a camera that does
        # not answer at all ends the query here.
        if error.error_code == "onvif_unreachable":
            raise
    else:
        camera_time = _camera_utc_time(clock)
        if camera_time is not None:
            offset = (camera_time - now()).total_seconds()

    capabilities_operation = _device_operation("GetCapabilities")
    category = ElementTree.SubElement(
        capabilities_operation,
        _tag(_DEVICE_NAMESPACE, "Category"),
    )
    category.text = "All"
    capabilities = call(
        device_service,
        _ACTION_GET_CAPABILITIES,
        capabilities_operation,
        authenticated=True,
        clock_offset_seconds=offset,
    )
    media_service = _service_address(capabilities, "Media", host)
    if not media_service:
        raise OnvifDiscoveryError("onvif_media_service_missing")
    ptz_endpoint = _service_address(capabilities, "PTZ", host)

    profiles_root = call(
        media_service,
        _ACTION_GET_PROFILES,
        ElementTree.Element(_tag(_MEDIA_NAMESPACE, "GetProfiles")),
        authenticated=True,
        clock_offset_seconds=offset,
    )
    listed: list[tuple[str, str, bool]] = []
    for element in profiles_root.findall(".//{*}Profiles"):
        token = (element.get("token") or "").strip()
        if not token or any(token == item[0] for item in listed):
            continue
        listed.append((
            token,
            (_child_text(element, "Name") or "").strip(),
            element.find("{*}PTZConfiguration") is not None,
        ))
        if len(listed) >= _MAXIMUM_PROFILES:
            break
    if not listed:
        raise OnvifDiscoveryError("onvif_profiles_missing")

    profiles: list[OnvifMediaProfile] = []
    for token, name, ptz in listed:
        try:
            stream_root = call(
                media_service,
                _ACTION_GET_STREAM_URI,
                _stream_uri_operation(token),
                authenticated=True,
                clock_offset_seconds=offset,
            )
        except OnvifDiscoveryError as error:
            # One profile without a stream (a metadata-only profile, say) must
            # not hide the others; a camera that stopped answering does.
            if error.error_code in {"onvif_unreachable", "onvif_authentication_failed"}:
                raise
            stream_uri = ""
        else:
            uri = stream_root.find(".//{*}MediaUri/{*}Uri")
            if uri is None:
                uri = stream_root.find(".//{*}Uri")
            stream_uri = _without_userinfo((uri.text or "").strip() if uri is not None else "")
        profiles.append(OnvifMediaProfile(token, name or token, stream_uri, ptz))
    return OnvifDeviceDetails(
        device_service=device_service,
        ptz_endpoint=ptz_endpoint,
        profiles=tuple(profiles),
    )


def _probe_device_service_ports(
    host: str,
    transport: OnvifTransport,
    cancellation: PtzCancellation,
) -> OnvifDiscoveredCamera | None:
    for port in ONVIF_FALLBACK_PORTS:
        if cancellation.cancelled:
            raise PtzCancelledError
        device_service = device_service_url(host, port)
        try:
            response = transport.call(
                endpoint=device_service,
                action=_ACTION_GET_SYSTEM_DATE_AND_TIME,
                payload=_soap_envelope(_device_operation("GetSystemDateAndTime"), None),
                credentials=None,
                timeout_seconds=_FALLBACK_CALL_SECONDS,
                cancellation=cancellation,
            )
        except PtzCancelledError:
            raise
        except Exception:  # noqa: BLE001 - a closed port is the expected answer
            continue
        root = _parse_xml(response.body)
        # Any SOAP envelope counts, a fault included: the camera may refuse
        # anonymous clock requests yet still be an ONVIF device.
        if root is None or root.tag.rsplit("}", 1)[-1] != "Envelope":
            continue
        return OnvifDiscoveredCamera(
            key=device_service,
            device_service=device_service,
            host=host,
            port=port,
        )
    return None


class OnvifDiscoveryService:
    """The discovery and query entry points with their production transports."""

    def __init__(
        self,
        *,
        discovery_transport: WsDiscoveryTransport | None = None,
        onvif_transport: OnvifTransport | None = None,
    ) -> None:
        self._discovery_transport = discovery_transport
        self._onvif_transport = onvif_transport

    def discover(
        self,
        cancellation: PtzCancellation | None = None,
    ) -> tuple[OnvifDiscoveredCamera, ...]:
        if self._discovery_transport is None:
            self._discovery_transport = UdpWsDiscoveryTransport()
        return discover_onvif_cameras(self._discovery_transport, cancellation=cancellation)

    def search(
        self,
        target: str,
        cancellation: PtzCancellation | None = None,
    ) -> tuple[OnvifDiscoveredCamera, ...]:
        if self._discovery_transport is None:
            self._discovery_transport = UdpWsDiscoveryTransport()
        return search_onvif_address(
            target,
            discovery_transport=self._discovery_transport,
            onvif_transport=self._onvif(),
            cancellation=cancellation,
        )

    def query(
        self,
        device_service: str,
        credentials: PtzCredentials | None,
        cancellation: PtzCancellation | None = None,
    ) -> OnvifDeviceDetails:
        return query_onvif_device(
            device_service,
            credentials,
            transport=self._onvif(),
            cancellation=cancellation,
        )

    def _onvif(self) -> OnvifTransport:
        if self._onvif_transport is None:
            from solin.core.scenes.ptz_onvif import AiohttpOnvifTransport

            self._onvif_transport = AiohttpOnvifTransport()
        return self._onvif_transport


_HOSTNAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_IPV4_RANGE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}-\d{1,3}(?:(?:\.\d{1,3}){3})?")


def _looks_like_ipv4_range(target: str) -> bool:
    return _IPV4_RANGE.fullmatch(target) is not None


def _octet(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 255:
        raise ValueError(text)
    return value


def _reachable_xaddr(xaddrs: Sequence[str], sender: str) -> str:
    """The advertised address that answered, or the first one moved to the sender.

    Cameras often advertise an address the probe did not reach them on (their
    DHCP lease on another subnet, or an address behind NAT). The sender of the
    reply is reachable by definition, so prefer an XAddr on it, and otherwise
    keep the first XAddr's path and port but point it at the sender — the same
    recovery ONVIF Device Manager applies.
    """
    candidates = [
        address for address in xaddrs
        if urlsplit(address).scheme in {"http", "https"}
    ]
    for address in candidates:
        if urlsplit(address).hostname == sender:
            return address
    if not candidates:
        return ""
    return _with_host(candidates[0], sender)


def _with_host(address: str, host: str) -> str:
    parsed = urlsplit(address)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _scope_values(scopes: Iterable[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for scope in scopes:
        if not scope.startswith(_SCOPE_PREFIX):
            continue
        kind, _, value = scope[len(_SCOPE_PREFIX):].partition("/")
        if kind in {"name", "hardware", "location"} and value and kind not in values:
            values[kind] = unquote(value).replace("_", " ").strip()
    return values


def _camera_sort_key(camera: OnvifDiscoveredCamera) -> tuple[int, int, str]:
    try:
        return (0, int(ipaddress.ip_address(camera.host)), camera.key)
    except ValueError:
        return (1, 0, camera.host + camera.key)


def _service_address(capabilities: ElementTree.Element, service: str, host: str) -> str:
    element = capabilities.find(f".//{{*}}{service}/{{*}}XAddr")
    if element is None:
        return ""
    address = (element.text or "").strip()
    parsed = urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    # The same advertised-address quirk as discovery: talk to the host that
    # answered, not the one the camera believes it has.
    if parsed.hostname != host:
        return _with_host(address, host)
    return address


def _camera_utc_time(root: ElementTree.Element) -> datetime | None:
    utc = root.find(".//{*}UTCDateTime")
    if utc is None:
        return None
    try:
        values = [
            int(_child_text(utc, path) or "")
            for path in (
                "Date/{*}Year", "Date/{*}Month", "Date/{*}Day",
                "Time/{*}Hour", "Time/{*}Minute", "Time/{*}Second",
            )
        ]
        return datetime(*values, tzinfo=UTC)
    except ValueError:
        return None


def _without_userinfo(uri: str) -> str:
    """Drop a login a camera embedded in a stream address; Solin stores it apart."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return uri
    if "@" not in parsed.netloc:
        return uri
    return urlunsplit((
        parsed.scheme,
        parsed.netloc.rpartition("@")[2],
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def _device_operation(name: str) -> ElementTree.Element:
    return ElementTree.Element(_tag(_DEVICE_NAMESPACE, name))


def _stream_uri_operation(profile_token: str) -> ElementTree.Element:
    operation = ElementTree.Element(_tag(_MEDIA_NAMESPACE, "GetStreamUri"))
    setup = ElementTree.SubElement(operation, _tag(_MEDIA_NAMESPACE, "StreamSetup"))
    stream = ElementTree.SubElement(setup, _tag(_SCHEMA_NAMESPACE, "Stream"))
    stream.text = "RTP-Unicast"
    transport = ElementTree.SubElement(setup, _tag(_SCHEMA_NAMESPACE, "Transport"))
    protocol = ElementTree.SubElement(transport, _tag(_SCHEMA_NAMESPACE, "Protocol"))
    protocol.text = "RTSP"
    token = ElementTree.SubElement(operation, _tag(_MEDIA_NAMESPACE, "ProfileToken"))
    token.text = profile_token
    return operation


def _checked_root(response: OnvifHttpResponse) -> ElementTree.Element:
    root = _parse_xml(response.body)
    fault = root.find(".//{*}Fault") if root is not None else None
    if response.status in {401, 403} or (fault is not None and _is_authorization_fault(fault)):
        raise OnvifDiscoveryError("onvif_authentication_failed")
    if fault is not None:
        raise OnvifDiscoveryError("onvif_fault")
    if not 200 <= response.status < 300 or root is None:
        raise OnvifDiscoveryError("onvif_response_invalid")
    return root


def _is_authorization_fault(fault: ElementTree.Element) -> bool:
    text = " ".join(" ".join(element.itertext()) for element in fault).casefold()
    return "notauthorized" in text or "not authorized" in text or "authority failure" in text


def _parse_xml(payload: bytes) -> ElementTree.Element | None:
    if not payload:
        return None
    try:
        return ElementTree.fromstring(_BARE_AMPERSAND.sub(b"&amp;", payload))
    except ElementTree.ParseError:
        return None


def _child_text(element: ElementTree.Element, path: str) -> str | None:
    child = element.find("{*}" + path)
    return child.text if child is not None else None


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"
