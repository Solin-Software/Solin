from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import socket
import threading
from xml.etree import ElementTree

import pytest

from solin.core.scenes import onvif_discovery
from solin.core.scenes.onvif_discovery import (
    OnvifDiscoveryError,
    OnvifDiscoveryService,
    UdpWsDiscoveryTransport,
    build_probe_message,
    device_service_url,
    discover_onvif_cameras,
    parse_probe_matches,
    parse_search_target,
    query_onvif_device,
    search_onvif_address,
)
from solin.core.scenes.ptz import PtzCancellation, PtzCancelledError, PtzCredentials, PtzExecutionError
from solin.core.scenes.ptz_onvif import OnvifHttpResponse


def _probe_match(
    *,
    uuid: str = "urn:uuid:camera-one",
    xaddrs: str = "http://192.168.1.20/onvif/device_service",
    scopes: str = (
        "onvif://www.onvif.org/name/Acme onvif://www.onvif.org/hardware/PTZ-200 "
        "onvif://www.onvif.org/location/Auditorium onvif://www.onvif.org/type/video_encoder"
    ),
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
          <s:Body><d:ProbeMatches><d:ProbeMatch>
            <a:EndpointReference><a:Address>{uuid}</a:Address></a:EndpointReference>
            <d:Types>dn:NetworkVideoTransmitter</d:Types>
            <d:Scopes>{scopes}</d:Scopes>
            <d:XAddrs>{xaddrs}</d:XAddrs>
          </d:ProbeMatch></d:ProbeMatches></s:Body>
        </s:Envelope>""".encode()


def test_probe_message_asks_for_video_transmitters_with_its_message_id() -> None:
    root = ElementTree.fromstring(build_probe_message("message-one"))

    assert root.find(".//{*}MessageID").text == "uuid:message-one"
    assert root.find(".//{*}Probe/{*}Types").text == "dn:NetworkVideoTransmitter"


def test_probe_match_reads_the_camera_identity_from_its_scopes() -> None:
    (camera,) = parse_probe_matches(_probe_match(), "192.168.1.20")

    assert camera.key == "urn:uuid:camera-one"
    assert camera.device_service == "http://192.168.1.20/onvif/device_service"
    assert (camera.host, camera.port) == ("192.168.1.20", 80)
    assert (camera.name, camera.hardware, camera.location) == ("Acme", "PTZ-200", "Auditorium")
    assert camera.display_name == "Acme PTZ-200"


def test_probe_match_prefers_the_advertised_address_that_answered() -> None:
    payload = _probe_match(
        xaddrs="http://10.0.0.9/onvif/device_service http://192.168.1.20:8080/onvif/device_service",
    )

    (camera,) = parse_probe_matches(payload, "192.168.1.20")

    assert camera.device_service == "http://192.168.1.20:8080/onvif/device_service"
    assert camera.port == 8080


def test_probe_match_moves_an_unreachable_advertised_address_to_the_sender() -> None:
    payload = _probe_match(xaddrs="http://10.0.0.9:8899/onvif/device_service")

    (camera,) = parse_probe_matches(payload, "192.168.1.20")

    assert camera.device_service == "http://192.168.1.20:8899/onvif/device_service"
    assert camera.host == "192.168.1.20"


def test_probe_match_tolerates_a_bare_ampersand() -> None:
    payload = _probe_match(scopes="onvif://www.onvif.org/name/Cam&Co")

    (camera,) = parse_probe_matches(payload, "192.168.1.20")

    assert camera.name == "Cam&Co"


@pytest.mark.parametrize(
    "payload",
    [b"", b"not xml", _probe_match(xaddrs=""), _probe_match(xaddrs="urn:not-http")],
)
def test_unusable_replies_yield_no_cameras(payload: bytes) -> None:
    assert parse_probe_matches(payload, "192.168.1.20") == ()


def test_scope_without_hardware_falls_back_to_the_host() -> None:
    (camera,) = parse_probe_matches(_probe_match(scopes=""), "192.168.1.20")

    assert camera.display_name == "192.168.1.20"


class _ProbeTransport:
    def __init__(self, replies: list[tuple[bytes, str]]) -> None:
        self.replies = replies
        self.messages: list[bytes] = []
        self.hosts: list[tuple[str, ...]] = []

    def probe(self, message: bytes, *, timeout_seconds: float, cancellation: PtzCancellation, hosts=()):
        self.messages.append(message)
        self.hosts.append(tuple(hosts))
        return self.replies


def test_discovery_merges_duplicate_replies_and_orders_by_address() -> None:
    transport = _ProbeTransport([
        (_probe_match(uuid="urn:uuid:b", xaddrs="http://192.168.1.30/onvif/device_service"), "192.168.1.30"),
        (_probe_match(uuid="urn:uuid:a", xaddrs="http://192.168.1.4/onvif/device_service"), "192.168.1.4"),
        (_probe_match(uuid="urn:uuid:b", xaddrs="http://192.168.1.30/onvif/device_service"), "192.168.1.30"),
    ])

    cameras = discover_onvif_cameras(transport, message_id="message-one")

    assert [camera.host for camera in cameras] == ["192.168.1.4", "192.168.1.30"]
    assert b"uuid:message-one" in transport.messages[0]


def test_udp_transport_collects_replies_sent_to_its_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback responder stands in for the multicast group."""
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    responder.settimeout(5)
    group_port = responder.getsockname()[1]
    seen: list[bytes] = []

    def answer() -> None:
        payload, sender = responder.recvfrom(65535)
        seen.append(payload)
        responder.sendto(b"reply", sender)

    worker = threading.Thread(target=answer)
    worker.start()
    monkeypatch.setattr(onvif_discovery, "WSD_MULTICAST_GROUP", ("127.0.0.1", group_port))
    transport = UdpWsDiscoveryTransport(interface_addresses=lambda: ("127.0.0.1",))
    try:
        replies = transport.probe(b"probe", timeout_seconds=0.5, cancellation=PtzCancellation())
    finally:
        worker.join()
        responder.close()

    assert seen == [b"probe"]
    assert replies == [(b"reply", "127.0.0.1")]


def test_udp_transport_stops_when_cancelled() -> None:
    cancellation = PtzCancellation()
    cancellation.cancel()
    transport = UdpWsDiscoveryTransport(interface_addresses=lambda: ("127.0.0.1",))

    with pytest.raises(PtzCancelledError):
        transport.probe(b"probe", timeout_seconds=5, cancellation=cancellation)


def _soap(body: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                    xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Body>{body}</s:Body>
        </s:Envelope>""".encode()


_CLOCK = _soap("""
    <tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>
      <tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>19</tt:Day></tt:Date>
      <tt:Time><tt:Hour>12</tt:Hour><tt:Minute>1</tt:Minute><tt:Second>40</tt:Second></tt:Time>
    </tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>
""")


def _capabilities(*, ptz: bool = True, media_host: str = "192.168.1.20") -> bytes:
    ptz_section = (
        "<tt:PTZ><tt:XAddr>http://10.0.0.9/onvif/ptz_service</tt:XAddr></tt:PTZ>" if ptz else ""
    )
    return _soap(f"""
        <tds:GetCapabilitiesResponse><tds:Capabilities>
          <tt:Device><tt:XAddr>http://{media_host}/onvif/device_service</tt:XAddr></tt:Device>
          <tt:Media><tt:XAddr>http://{media_host}/onvif/media_service</tt:XAddr></tt:Media>
          {ptz_section}
        </tds:Capabilities></tds:GetCapabilitiesResponse>
    """)


_PROFILES = _soap("""
    <trt:GetProfilesResponse>
      <trt:Profiles token="main" fixed="true"><tt:Name>MainStream</tt:Name>
        <tt:PTZConfiguration token="ptz0"><tt:Name>PTZ</tt:Name></tt:PTZConfiguration>
      </trt:Profiles>
      <trt:Profiles token="sub"><tt:Name>SubStream</tt:Name></trt:Profiles>
    </trt:GetProfilesResponse>
""")


def _stream(uri: str) -> bytes:
    return _soap(
        f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


@dataclass
class _Call:
    endpoint: str
    action: str
    payload: bytes
    credentials: PtzCredentials | None


class _OnvifTransport:
    def __init__(self, responses: list[OnvifHttpResponse | Exception]) -> None:
        self.responses = deque(responses)
        self.calls: list[_Call] = []

    def call(self, *, endpoint, action, payload, credentials, timeout_seconds, cancellation):
        self.calls.append(_Call(endpoint, action, payload, credentials))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _ok(body: bytes) -> OnvifHttpResponse:
    return OnvifHttpResponse(200, body)


def _query(transport: _OnvifTransport, credentials: PtzCredentials | None = None):
    return query_onvif_device(
        "http://192.168.1.20/onvif/device_service",
        credentials,
        transport=transport,
        now=lambda: datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC),
    )


def test_query_returns_streams_and_the_ptz_service() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK),
        _ok(_capabilities()),
        _ok(_PROFILES),
        # Xiongmai-style: unescaped '&' and a login inside the address.
        _ok(_stream("rtsp://admin:secret@192.168.1.20:554/user=admin&channel=1&stream=0")),
        _ok(_stream("rtsp://192.168.1.20:554/sub")),
    ])

    details = _query(transport)

    assert details.ptz_endpoint == "http://192.168.1.20/onvif/ptz_service"
    assert [(p.token, p.name, p.ptz) for p in details.profiles] == [
        ("main", "MainStream", True),
        ("sub", "SubStream", False),
    ]
    assert details.profiles[0].stream_uri == "rtsp://192.168.1.20:554/user=admin&channel=1&stream=0"
    assert details.profiles[1].stream_uri == "rtsp://192.168.1.20:554/sub"
    assert [call.endpoint.rsplit("/", 1)[1] for call in transport.calls] == [
        "device_service", "device_service", "media_service", "media_service", "media_service",
    ]


def test_anonymous_query_sends_no_login() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities()), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])

    _query(transport)

    assert all(call.credentials is None for call in transport.calls)
    assert all(b"UsernameToken" not in call.payload for call in transport.calls)


def test_query_signs_requests_with_the_camera_clock() -> None:
    credentials = PtzCredentials("admin", "secret")
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities()), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])

    _query(transport, credentials)

    assert transport.calls[0].credentials is None
    capabilities = ElementTree.fromstring(transport.calls[1].payload)
    created = capabilities.find(".//{*}UsernameToken/{*}Created").text
    created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    # The camera runs 100 s ahead of the fake local clock; the real local clock
    # is used for "now" in the header, so compare against the offset only.
    offset = (created_at - datetime.now(UTC)).total_seconds()
    assert 95 <= offset <= 105
    assert transport.calls[1].credentials == credentials


def test_query_keeps_the_media_service_on_the_host_that_answered() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities(media_host="10.0.0.9")), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])

    _query(transport)

    assert transport.calls[2].endpoint == "http://192.168.1.20/onvif/media_service"


def test_query_without_ptz_service_reports_none() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities(ptz=False)), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])

    assert _query(transport).ptz_endpoint == ""


def test_a_profile_without_a_stream_does_not_hide_the_others() -> None:
    fault = _soap("<s:Fault><s:Reason><s:Text>No stream</s:Text></s:Reason></s:Fault>")
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities()), _ok(_PROFILES),
        OnvifHttpResponse(500, fault), _ok(_stream("rtsp://cam/sub")),
    ])

    details = _query(transport)

    assert [profile.stream_uri for profile in details.profiles] == ["", "rtsp://cam/sub"]


@pytest.mark.parametrize(
    "response",
    [
        OnvifHttpResponse(401, b""),
        OnvifHttpResponse(400, _soap(
            "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
            "<s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code></s:Fault>"
        )),
    ],
)
def test_rejected_login_is_reported_as_an_authentication_failure(response) -> None:
    transport = _OnvifTransport([_ok(_CLOCK), response])

    with pytest.raises(OnvifDiscoveryError) as caught:
        _query(transport)

    assert caught.value.error_code == "onvif_authentication_failed"


def test_a_camera_that_does_not_answer_is_unreachable() -> None:
    transport = _OnvifTransport([PtzExecutionError("ptz_network_unavailable")])

    with pytest.raises(OnvifDiscoveryError) as caught:
        _query(transport)

    assert caught.value.error_code == "onvif_unreachable"


def test_a_refused_clock_request_does_not_end_the_query() -> None:
    transport = _OnvifTransport([
        OnvifHttpResponse(401, b""), _ok(_capabilities()), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])

    assert len(_query(transport).profiles) == 2


def test_a_camera_without_profiles_is_reported() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities()), _ok(_soap("<trt:GetProfilesResponse/>")),
    ])

    with pytest.raises(OnvifDiscoveryError) as caught:
        _query(transport)

    assert caught.value.error_code == "onvif_profiles_missing"


def test_a_camera_without_media_service_is_reported() -> None:
    transport = _OnvifTransport([
        _ok(_CLOCK),
        _ok(_soap("<tds:GetCapabilitiesResponse><tds:Capabilities/></tds:GetCapabilitiesResponse>")),
    ])

    with pytest.raises(OnvifDiscoveryError) as caught:
        _query(transport)

    assert caught.value.error_code == "onvif_media_service_missing"


def test_query_rejects_an_address_that_is_not_http() -> None:
    with pytest.raises(OnvifDiscoveryError) as caught:
        query_onvif_device("rtsp://cam/stream", None, transport=_OnvifTransport([]))

    assert caught.value.error_code == "onvif_address_invalid"


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("192.168.1.20", 80, "http://192.168.1.20/onvif/device_service"),
        ("camera.local", 8899, "http://camera.local:8899/onvif/device_service"),
        ("fe80::1", 80, "http://[fe80::1]/onvif/device_service"),
    ],
)
def test_device_service_url_for_a_known_host(host: str, port: int, expected: str) -> None:
    assert device_service_url(host, port) == expected


def test_service_uses_injected_transports() -> None:
    probe = _ProbeTransport([(_probe_match(), "192.168.1.20")])
    onvif = _OnvifTransport([
        _ok(_CLOCK), _ok(_capabilities()), _ok(_PROFILES),
        _ok(_stream("rtsp://cam/main")), _ok(_stream("rtsp://cam/sub")),
    ])
    service = OnvifDiscoveryService(discovery_transport=probe, onvif_transport=onvif)

    (camera,) = service.discover()
    details = service.query(camera.device_service, None)

    assert details.device_service == camera.device_service
    assert len(details.profiles) == 2


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("192.168.0.100", ("192.168.0.100",)),
        (" camera.local ", ("camera.local",)),
        ("192.168.0.10-12", ("192.168.0.10", "192.168.0.11", "192.168.0.12")),
        ("192.168.0.254-192.168.1.1", ("192.168.0.254", "192.168.0.255", "192.168.1.0", "192.168.1.1")),
        ("192.168.0.0/30", ("192.168.0.1", "192.168.0.2")),
        ("192.168.0.7/32", ("192.168.0.7",)),
    ],
)
def test_search_target_names_its_hosts(target: str, expected: tuple[str, ...]) -> None:
    assert parse_search_target(target) == expected


def test_search_target_accepts_up_to_a_slash_22() -> None:
    assert len(parse_search_target("10.0.0.0/22")) == 1022


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("", "onvif_search_target_invalid"),
        ("192.168.0.300", "onvif_search_target_invalid"),
        ("192.168.0.50-10", "onvif_search_target_invalid"),
        ("192.168.0.1-300", "onvif_search_target_invalid"),
        ("two words", "onvif_search_target_invalid"),
        ("http://camera/", "onvif_search_target_invalid"),
        ("10.0.0.0/16", "onvif_search_target_too_large"),
        ("10.0.0.1-10.0.8.1", "onvif_search_target_too_large"),
    ],
)
def test_search_target_rejects_what_it_cannot_sweep(target: str, code: str) -> None:
    with pytest.raises(OnvifDiscoveryError) as caught:
        parse_search_target(target)

    assert caught.value.error_code == code


def test_address_search_probes_each_host_directly() -> None:
    probe = _ProbeTransport([(_probe_match(xaddrs="http://192.168.0.5/onvif/device_service"), "192.168.0.5")])

    cameras = search_onvif_address(
        "192.168.0.4-6", discovery_transport=probe, onvif_transport=_OnvifTransport([]),
    )

    assert probe.hosts == [("192.168.0.4", "192.168.0.5", "192.168.0.6")]
    assert [camera.host for camera in cameras] == ["192.168.0.5"]


def test_a_single_address_that_ignores_probes_is_found_on_a_usual_port() -> None:
    onvif = _OnvifTransport([
        PtzExecutionError("ptz_network_unavailable"),  # port 80
        OnvifHttpResponse(400, _soap("<s:Fault/>")),  # port 8899 refuses, but speaks SOAP
    ])

    (camera,) = search_onvif_address(
        "192.168.0.100", discovery_transport=_ProbeTransport([]), onvif_transport=onvif,
    )

    assert camera.device_service == "http://192.168.0.100:8899/onvif/device_service"
    assert (camera.host, camera.port) == ("192.168.0.100", 8899)
    assert [call.endpoint for call in onvif.calls] == [
        "http://192.168.0.100/onvif/device_service",
        "http://192.168.0.100:8899/onvif/device_service",
    ]


def test_a_single_address_answering_without_soap_is_not_a_camera() -> None:
    onvif = _OnvifTransport([OnvifHttpResponse(200, b"<html/>")] * 5)

    cameras = search_onvif_address(
        "192.168.0.100", discovery_transport=_ProbeTransport([]), onvif_transport=onvif,
    )

    assert cameras == ()
    assert len(onvif.calls) == 5


def test_udp_sweep_sends_to_each_host_and_collects_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    responder.settimeout(5)
    monkeypatch.setattr(onvif_discovery, "WSD_PORT", responder.getsockname()[1])
    seen: list[bytes] = []

    def answer() -> None:
        # Two hosts that both resolve to this responder: two probes arrive.
        for _ in range(2):
            payload, sender = responder.recvfrom(65535)
            seen.append(payload)
            responder.sendto(b"reply", sender)

    worker = threading.Thread(target=answer)
    worker.start()
    try:
        replies = UdpWsDiscoveryTransport().probe(
            b"probe", timeout_seconds=0.5, cancellation=PtzCancellation(),
            hosts=("127.0.0.1", "localhost"),
        )
    finally:
        worker.join()
        responder.close()

    assert seen == [b"probe", b"probe"]
    assert replies == [(b"reply", "127.0.0.1"), (b"reply", "127.0.0.1")]
