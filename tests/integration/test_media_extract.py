from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

import pytest

from solin.core.media.media_extract import (
    ImagePolicy,
    MediaExtractCancelled,
    MediaExtractError,
    MediaExtractRequest,
    MediaExtractService,
    media_extract_executable,
)
from tests._http import ThreadingLoopbackHTTPServer

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "media_extract"


def test_headless_client_qualification_without_qt_obs_or_user_state(native_media_service, tmp_path):
    from solin.bootstrap.media_runtime_verification import MEDIA_RUNTIME_CHECKS
    from tests._paths import REPO_ROOT

    fixtures = tmp_path / "canção – testes 🎵"
    shutil.copytree(_FIXTURES, fixtures)
    work = tmp_path / "isolated"
    work.mkdir()
    code = (
        "import runpy,sys; sys.modules['PySide6']=None; "
        "sys.modules['solin.bootstrap.application']=None; "
        f"sys.argv=['Solin','--verify-media-runtime',{str(fixtures)!r}]; "
        f"runpy.run_path({str(REPO_ROOT / 'main.py')!r},run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=15,
        cwd=work,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout) == {
        "schema": "solin.media-runtime.v1",
        "ok": True,
        "checks": list(MEDIA_RUNTIME_CHECKS),
    }
    assert list(work.iterdir()) == []


@pytest.fixture
def native_media_service(monkeypatch):
    try:
        media_extract_executable()
    except MediaExtractError:
        if os.environ.get("CI"):
            pytest.fail("CI must build and stage the native media extraction helper")
        pytest.skip("Build the native media extraction helper before real-media tests")
    monkeypatch.setenv("PATH", "")
    service = MediaExtractService()
    try:
        yield service
    finally:
        service.close()
    assert service.active_count == 0


def _center(image):
    assert image is not None
    offset = (image.height // 2) * image.stride + (image.width // 2) * 4
    return tuple(image.data[offset : offset + 4])


def test_real_video_metadata_and_frame_without_external_commands(native_media_service, tmp_path):
    path = tmp_path / "canção d'água – apresentação.mp4"
    shutil.copyfile(_FIXTURES / "video.mp4", path)
    result = native_media_service.extract(MediaExtractRequest(str(path)))
    assert result.title == "Canção — ação 🎵"
    assert result.duration_ms == 2000
    assert result.has_video
    assert result.image is not None and result.image.kind == "frame"
    blue, green, red, alpha = _center(result.image)
    assert blue < 20 and green < 20 and red > 230 and alpha == 255


@pytest.mark.parametrize("name", ["audio-cover.mp3", "video-cover.mp4"])
def test_attached_cover_precedes_video_frame(native_media_service, name):
    result = native_media_service.extract(MediaExtractRequest(str(_FIXTURES / name)))
    assert result.image is not None and result.image.kind == "cover"
    blue, green, red, alpha = _center(result.image)
    assert blue < 25 and red < 25 and green > 190 and alpha == 255


def test_audio_without_cover_is_successful_absence(native_media_service):
    result = native_media_service.extract(
        MediaExtractRequest(
            str(_FIXTURES / "audio.mp3"),
            image_policy=ImagePolicy.COVER_ONLY,
        )
    )
    assert result.title == "Áudio sem capa"
    assert result.duration_ms > 400
    assert not result.has_video
    assert result.image is None


def test_cover_only_does_not_substitute_video_frame(native_media_service):
    result = native_media_service.extract(
        MediaExtractRequest(
            str(_FIXTURES / "video.mp4"),
            image_policy=ImagePolicy.COVER_ONLY,
        )
    )
    assert result.has_video
    assert result.image is None


def test_metadata_only_does_not_return_pixels(native_media_service):
    result = native_media_service.extract(
        MediaExtractRequest(
            str(_FIXTURES / "video-cover.mp4"),
            image_policy=ImagePolicy.NONE,
        )
    )
    assert result.duration_ms == 2000
    assert result.image is None


@pytest.mark.parametrize(
    "name,aspect",
    [
        ("video-rotated.mp4", 3 / 4),
        ("video-sar.mp4", 8 / 3),
        ("video-short.mp4", 4 / 3),
    ],
)
def test_display_orientation_aspect_and_short_video(native_media_service, name, aspect):
    result = native_media_service.extract(MediaExtractRequest(str(_FIXTURES / name)))
    assert result.image is not None
    assert result.image.width / result.image.height == pytest.approx(aspect, abs=0.02)
    assert result.image.width <= 640 and result.image.height <= 360


def test_corrupt_file_is_not_cached_as_missing_artwork(native_media_service, tmp_path):
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"this is not a media container")
    with pytest.raises(MediaExtractError) as caught:
        native_media_service.extract(MediaExtractRequest(str(path)))
    assert caught.value.failure.kind.value in {"format", "permanent"}


def test_unicode_title_reaches_playback_metadata_signal(
    native_media_service, tmp_path, monkeypatch, qt_object_owner
):
    from solin.core.media import routed_metadata

    path = tmp_path / "canção d'água – apresentação.mp4"
    shutil.copyfile(_FIXTURES / "video.mp4", path)
    monkeypatch.setattr(routed_metadata, "get_media_extract_service", lambda: native_media_service)
    results = []
    extractor = routed_metadata.RoutedMediaMetadataExtractor(
        qt_object_owner,
        runner=lambda work: work(),
    )
    extractor.metadata_ready.connect(lambda *args: results.append(args))
    extractor.request(7, str(path))
    assert results == [(7, "Canção — ação 🎵", None)]
    extractor.cancel()


@contextmanager
def _http_media(
    payload: bytes,
    *,
    stalled: bool = False,
    seekable: bool = True,
    tls_context: ssl.SSLContext | None = None,
):
    stopped = threading.Event()
    requested = threading.Event()
    totals = {"bytes": 0, "ranges": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802 - HTTP handler contract
            requested.set()
            if stalled:
                stopped.wait(5)
                return
            if self.path == "/unavailable":
                self.send_error(503)
                return
            if self.path == "/missing":
                self.send_error(404)
                return
            if self.path.startswith("/status/"):
                self.send_error(int(self.path.rsplit("/", 1)[1]))
                return
            start, end = 0, len(payload) - 1
            requested_range = self.headers.get("Range", "")
            if seekable and requested_range.startswith("bytes="):
                raw_start, raw_end = requested_range.removeprefix("bytes=").split("-", 1)
                start = int(raw_start or 0)
                end = min(end, int(raw_end)) if raw_end else end
                with lock:
                    totals["ranges"] += 1
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", "video/mp4")
            if seekable:
                self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                for offset in range(start, end + 1, 4096):
                    if stopped.is_set():
                        break
                    chunk = payload[offset : min(offset + 4096, end + 1)]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    with lock:
                        totals["bytes"] += len(chunk)
                    # Bound kernel prefetch so a tiny thumbnail read does not
                    # accidentally transmit the whole padded fixture on loopback.
                    stopped.wait(0.002)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Cancellation and seeks close the previous HTTP connection.

    server = ThreadingLoopbackHTTPServer(Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if tls_context is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}", totals, requested
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_http_fixture_does_not_discover_network_hostnames(monkeypatch):
    def forbidden_discovery(*_args):
        raise AssertionError("Media HTTP fixtures must not discover local network hosts")

    monkeypatch.setattr(socket, "getfqdn", forbidden_discovery)
    monkeypatch.setattr(socket, "gethostname", forbidden_discovery)
    with _http_media(b"test") as (url, _totals, _requested):
        assert url.startswith("http://127.0.0.1:")


def test_http_frame_reads_ranges_without_full_download(native_media_service):
    padding = 4 * 1024 * 1024
    payload = (
        (_FIXTURES / "video.mp4").read_bytes()
        + struct.pack(">I4s", padding + 8, b"free")
        + b"\0" * padding
    )
    with _http_media(payload) as (origin, totals, _requested):
        result = native_media_service.extract(MediaExtractRequest(origin + "/video.mp4"))
        assert result.duration_ms == 2000
        assert result.image is not None and result.image.kind == "frame"
        assert totals["ranges"] > 0
        assert 0 < totals["bytes"] < len(payload)


def test_nonseekable_http_source_can_decode_forward(native_media_service):
    with _http_media((_FIXTURES / "video.mp4").read_bytes(), seekable=False) as (
        origin,
        _,
        _requested,
    ):
        result = native_media_service.extract(MediaExtractRequest(origin + "/video.mp4"))
        assert result.image is not None
        assert result.duration_ms == 2000


def test_https_source_uses_native_tls_without_external_commands(native_media_service, tmp_path):
    from datetime import datetime, timedelta, timezone
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_file, key_file = tmp_path / "server.pem", tmp_path / "server-key.pem"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    with _http_media((_FIXTURES / "video.mp4").read_bytes(), tls_context=context) as (
        origin,
        totals,
        _,
    ):
        result = native_media_service.extract(MediaExtractRequest(origin + "/video.mp4"))
        assert result.duration_ms == 2000
        assert result.image is not None and result.image.kind == "frame"
        assert totals["bytes"] > 0


def test_cancel_interrupts_stalled_origin_and_reaps_child(native_media_service):
    cancel = threading.Event()
    with _http_media(b"", stalled=True) as (origin, _totals, requested):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                native_media_service.extract,
                MediaExtractRequest(origin + "/video.mp4"),
                cancel=cancel,
            )
            assert requested.wait(3)
            started = time.monotonic()
            cancel.set()
            with pytest.raises(MediaExtractCancelled):
                future.result(timeout=3)
            assert time.monotonic() - started < 2
        assert native_media_service.active_count == 0


@pytest.mark.parametrize(
    "location,kind",
    [("/unavailable", "transient"), ("/missing", "permanent")]
    + [(f"/status/{status}", "transient") for status in (408, 409, 423, 425, 429)]
    + [(f"/status/{status}", "permanent") for status in (401, 403)],
)
def test_http_failure_classification(native_media_service, location, kind):
    with _http_media(b"") as (origin, _totals, _requested):
        with pytest.raises(MediaExtractError) as caught:
            native_media_service.extract(MediaExtractRequest(origin + location))
        assert caught.value.failure.kind.value == kind
