import pytest
import requests

from solin.core.network import http


def test_get_json_uses_requests_with_timeout_and_headers(monkeypatch) -> None:
    calls = []

    class _Response:
        status_code = 200
        url = "https://example.test/data.json"
        headers = {"Content-Type": "application/json"}
        content = b'{"ok": true}'

        def raise_for_status(self):
            return None

    def fake_get(url, *, headers, params, timeout, stream, verify):
        calls.append((url, headers, params, timeout, stream, bool(verify)))
        return _Response()

    monkeypatch.setattr(requests, "get", fake_get)

    assert http.get_json(
        "https://example.test/data.json",
        timeout=12,
        headers={"User-Agent": "Solin"},
    ) == {"ok": True}
    assert calls == [
        (
            "https://example.test/data.json",
            {"User-Agent": "Solin"},
            None,
            12,
            False,
            True,
        )
    ]


def test_get_json_wraps_transport_errors(monkeypatch) -> None:
    def fake_get(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(http.HttpTransportError):
        http.get_json("https://example.test/data.json", timeout=12)


def test_get_json_wraps_status_errors(monkeypatch) -> None:
    class _Response:
        status_code = 404
        url = "https://example.test/missing.json"
        headers = {}
        content = b""

        def raise_for_status(self):
            raise requests.HTTPError("not found")

        def close(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: _Response())

    with pytest.raises(http.HttpStatusError) as exc_info:
        http.get_json("https://example.test/missing.json", timeout=12)

    assert exc_info.value.status_code == 404


def test_get_json_wraps_decode_errors(monkeypatch) -> None:
    class _Response:
        status_code = 200
        url = "https://example.test/data.json"
        headers = {}
        content = b"{bad"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: _Response())

    with pytest.raises(http.HttpDecodeError):
        http.get_json("https://example.test/data.json", timeout=12)


def test_stream_get_yields_bytes_and_closes_response(monkeypatch) -> None:
    class _Response:
        status_code = 200
        url = "https://example.test/media.mp4"
        headers = {"Content-Length": "6"}
        closed = False

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            assert chunk_size == 3
            yield b"abc"
            yield b""
            yield b"def"

        def close(self):
            self.closed = True

    response = _Response()
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)

    with http.stream_get("https://example.test/media.mp4", timeout=12) as stream:
        assert stream.content_length == 6
        assert list(stream.iter_bytes(chunk_size=3)) == [b"abc", b"def"]

    assert response.closed is True


def test_get_bytes_enforces_response_limit(monkeypatch) -> None:
    class _Response:
        status_code = 200
        url = "https://example.test/image.jpg"
        headers = {}

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            yield b"123"
            yield b"456"

        def close(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: _Response())

    with pytest.raises(http.HttpResponseTooLargeError):
        http.get_bytes("https://example.test/image.jpg", timeout=12, max_bytes=5)


def _post_response(status, content):
    class _Response:
        url = "http://127.0.0.1:7880/twirp/x"
        headers = {"Content-Type": "application/json"}

    response = _Response()
    response.status_code = status
    response.content = content
    return response


def test_post_json_sends_json_and_decodes_the_reply(monkeypatch) -> None:
    calls = []

    def fake_post(url, *, json, headers, params, timeout, verify):
        calls.append((url, json, headers, timeout))
        return _post_response(200, b'{"participants": []}')

    monkeypatch.setattr(requests, "post", fake_post)
    reply = http.post_json("http://127.0.0.1:7880/twirp/x", {"room": "r"}, timeout=3,
                           headers={"Authorization": "Bearer t"})
    assert reply == {"participants": []}
    assert calls == [("http://127.0.0.1:7880/twirp/x", {"room": "r"},
                      {"Authorization": "Bearer t"}, 3)]


def test_post_json_returns_none_for_an_empty_body(monkeypatch) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **k: _post_response(200, b""))
    assert http.post_json("http://127.0.0.1/x", {}) is None


def test_post_json_reports_status_and_transport_errors(monkeypatch) -> None:
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _post_response(404, b'{"code":"not_found"}'))
    with pytest.raises(http.HttpStatusError, match="POST .* 404: .*not_found") as error:
        http.post_json("http://127.0.0.1/x", {})
    assert error.value.status_code == 404

    def offline(*_a, **_k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", offline)
    with pytest.raises(http.HttpTransportError):
        http.post_json("http://127.0.0.1/x", {})
