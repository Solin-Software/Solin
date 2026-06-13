from __future__ import annotations

import http.client
import threading
from http.server import HTTPServer

from solin.core.foundation import paths as app_paths
from solin.core.ingest.wifi_server import _make_handler, _parse_multipart, _safe_filename


def _multipart(filename: str, payload: bytes = b"data") -> tuple[bytes, str]:
    boundary = "----solin-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, boundary


def test_parse_multipart_sanitizes_uploaded_filename():
    body, boundary = _multipart(r"..\..\evil?.mp4", b"payload")

    parts = _parse_multipart(body, boundary.encode("ascii"))

    assert parts == [
        {
            "filename": "evil_.mp4",
            "data": b"payload",
            "content_type": "application/octet-stream",
        }
    ]


def test_safe_filename_rejects_path_traversal_shape():
    assert _safe_filename("../../secret.mp4") == "secret.mp4"
    assert _safe_filename(r"..\..\evil?.mp4") == "evil_.mp4"
    assert _safe_filename("...") == "upload"


def test_wifi_upload_rejects_disallowed_extension_server_side(tmp_path, monkeypatch):
    received: list[tuple[str, str]] = []
    monkeypatch.setattr(app_paths, "EMBEDDED_DIR", str(tmp_path))
    handler = _make_handler(
        token="token",
        html="ok",
        on_file=lambda path, name: received.append((path, name)),
        on_activity=lambda: None,
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
