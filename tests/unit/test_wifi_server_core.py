from __future__ import annotations

import http.client
import threading
from http.server import HTTPServer
from pathlib import Path

from solin.core.ingest.wifi_server import make_handler
from solin.core.ingest.wifi_uploads import (
    is_allowed_upload_filename,
    parse_multipart,
    safe_filename,
)


def _multipart(filename: str, payload: bytes = b"data") -> tuple[bytes, str]:
    boundary = "----solin-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, boundary


def testparse_multipart_sanitizes_uploaded_filename():
    body, boundary = _multipart(r"..\..\evil?.mp4", b"payload")

    parts = parse_multipart(body, boundary.encode("ascii"))

    assert parts == [
        {
            "filename": "evil_.mp4",
            "data": b"payload",
            "content_type": "application/octet-stream",
        }
    ]


def testsafe_filename_rejects_path_traversal_shape():
    assert safe_filename("../../secret.mp4") == "secret.mp4"
    assert safe_filename(r"..\..\evil?.mp4") == "evil_.mp4"
    assert safe_filename("...") == "upload"


def test_upload_filename_policy_allows_only_supported_media_and_playlist_files():
    assert is_allowed_upload_filename("clip.mp4") is True
    assert is_allowed_upload_filename("playlist.jwlplaylist") is True
    assert is_allowed_upload_filename("publication.jwpub") is True
    assert is_allowed_upload_filename("tool.exe") is False


def test_wifi_upload_rejects_disallowed_extension_server_side(tmp_path):
    received: list[tuple[str, str]] = []
    handler = make_handler(
        token="token",
        html="ok",
        on_file=lambda path, name: received.append((path, name)),
        on_activity=lambda: None,
        embedded_dir=tmp_path,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, boundary = _multipart("tool.exe", b"not-media")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/token",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = conn.getresponse()
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 422
    assert received == []
    assert list(tmp_path.iterdir()) == []


def test_wifi_upload_saves_allowed_media_in_injected_directory(tmp_path):
    embedded_dir = tmp_path / "profile" / "embedded"
    received: list[tuple[str, str]] = []
    handler = make_handler(
        token="token",
        html="ok",
        on_file=lambda path, name: received.append((path, name)),
        on_activity=lambda: None,
        embedded_dir=embedded_dir,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, boundary = _multipart("clip.mp4", b"media-bytes")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/token",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = conn.getresponse()
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert len(received) == 1
    saved_path, original_name = received[0]
    assert original_name == "clip.mp4"
    assert Path(saved_path).parent == embedded_dir
    assert Path(saved_path).read_bytes() == b"media-bytes"
