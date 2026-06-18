from __future__ import annotations

from solin.core.jw import memorial_publication
from solin.core.jw.memorial_publication import resolve_memorial_jwpub
from solin.core.network import http


def _jwpub_payload(url: str, *, checksum: str = "abc", thumb: str = "thumb.jpg"):
    return {
        "files": {
            "T": {
                "JWPUB": [
                    {
                        "file": {"url": url, "checksum": checksum},
                        "images": {"sqr": {"sm": {"url": thumb}}},
                    }
                ]
            }
        }
    }


def test_resolve_memorial_jwpub_uses_direct_api_payload(monkeypatch):
    monkeypatch.setattr(
        memorial_publication,
        "fetch_pub_media_json",
        lambda params: _jwpub_payload("https://example.test/direct.jwpub"),
    )
    monkeypatch.setattr(
        memorial_publication,
        "http_get_json",
        lambda _url: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    info = resolve_memorial_jwpub("mi26", "T")

    assert info.download_url == "https://example.test/direct.jwpub"
    assert info.thumbnail_url == "thumb.jpg"
    assert info.checksum == "abc"
    assert info.not_found is False


def test_resolve_memorial_jwpub_uses_fallback_api_payload(monkeypatch):
    monkeypatch.setattr(
        memorial_publication,
        "fetch_pub_media_json",
        lambda _params: None,
    )
    monkeypatch.setattr(
        memorial_publication,
        "http_get_json",
        lambda _url: _jwpub_payload("https://example.test/fallback.jwpub"),
    )

    info = resolve_memorial_jwpub("mi26", "T")

    assert info.download_url == "https://example.test/fallback.jwpub"
    assert info.not_found is False


def test_resolve_memorial_jwpub_marks_not_found_after_empty_api_response(monkeypatch):
    monkeypatch.setattr(
        memorial_publication,
        "fetch_pub_media_json",
        lambda _params: {"files": {}},
    )
    monkeypatch.setattr(
        memorial_publication,
        "http_get_json",
        lambda _url: None,
    )

    info = resolve_memorial_jwpub("mi26", "T")

    assert info.download_url is None
    assert info.not_found is True


def test_download_memorial_bytes_prefers_browser_impersonating_transport():
    calls: list[tuple[str, str, dict[str, str]]] = []

    class _BrowserTransport:
        def get(self, request: http.HttpRequest) -> http.HttpResponse:
            calls.append(("browser", request.url, dict(request.headers)))
            return http.HttpResponse(
                url=request.url,
                status_code=200,
                headers=http.HttpHeaders({}),
                content=b"jwpub",
            )

        def stream(self, request: http.HttpRequest) -> http.HttpByteStream:
            raise AssertionError("unexpected stream")

    class _FallbackTransport:
        def get(self, request: http.HttpRequest) -> http.HttpResponse:
            raise AssertionError("unexpected fallback")

        def stream(self, request: http.HttpRequest) -> http.HttpByteStream:
            raise AssertionError("unexpected stream")

    data = memorial_publication.download_memorial_bytes(
        "https://example.test/archive.jwpub",
        browser_transport=_BrowserTransport(),
        fallback_transport=_FallbackTransport(),
    )

    assert data == b"jwpub"
    assert calls == [("browser", "https://example.test/archive.jwpub", {})]


def test_download_memorial_bytes_uses_fallback_when_impersonation_is_unavailable():
    calls: list[tuple[str, str, dict[str, str]]] = []

    class _BrowserTransport:
        def get(self, request: http.HttpRequest) -> http.HttpResponse:
            calls.append(("browser", request.url, dict(request.headers)))
            raise http.HttpBrowserImpersonationUnavailableError("missing optional adapter")

        def stream(self, request: http.HttpRequest) -> http.HttpByteStream:
            raise AssertionError("unexpected stream")

    class _FallbackTransport:
        def get(self, request: http.HttpRequest) -> http.HttpResponse:
            calls.append(("fallback", request.url, dict(request.headers)))
            return http.HttpResponse(
                url=request.url,
                status_code=200,
                headers=http.HttpHeaders({}),
                content=b"fallback-jwpub",
            )

        def stream(self, request: http.HttpRequest) -> http.HttpByteStream:
            raise AssertionError("unexpected stream")

    data = memorial_publication.download_memorial_bytes(
        "https://example.test/archive.jwpub",
        browser_transport=_BrowserTransport(),
        fallback_transport=_FallbackTransport(),
    )

    assert data == b"fallback-jwpub"
    assert calls == [
        ("browser", "https://example.test/archive.jwpub", {}),
        ("fallback", "https://example.test/archive.jwpub", {"User-Agent": "Mozilla/5.0"}),
    ]
