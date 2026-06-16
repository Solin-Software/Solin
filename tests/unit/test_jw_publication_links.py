from solin.core.jw import publication_links
from solin.core.jw.publication_links import (
    VIDEO_FORMATS,
    build_pub_media_url,
    resolve_publication_video_link,
    select_pub_media_file,
)


def test_select_pub_media_file_uses_requested_language_payload() -> None:
    data = {
        "files": {
            "T": {
                "JWPUB": [
                    {
                        "file": {
                            "url": "https://example.test/mi26_T.jwpub",
                            "checksum": "abc123",
                        },
                        "images": {
                            "sqr": {
                                "sm": {"url": "https://example.test/thumb.jpg"},
                            }
                        },
                    }
                ]
            }
        }
    }

    media_file = select_pub_media_file(data, "T", ("JWPUB",), fallback_languages=("E",))

    assert media_file is not None
    assert media_file.url == "https://example.test/mi26_T.jwpub"
    assert media_file.thumbnail_url == "https://example.test/thumb.jpg"
    assert media_file.checksum == "abc123"


def test_select_pub_media_file_falls_back_to_english_payload() -> None:
    data = {
        "files": {
            "E": {
                "JWPUB": [
                    {
                        "file": {"url": "https://example.test/mi26_E.jwpub"},
                        "images": {"wss": {"md": {"url": "https://example.test/wss.jpg"}}},
                    }
                ]
            }
        }
    }

    media_file = select_pub_media_file(data, "T", ("JWPUB",), fallback_languages=("E",))

    assert media_file is not None
    assert media_file.url == "https://example.test/mi26_E.jwpub"
    assert media_file.thumbnail_url == "https://example.test/wss.jpg"
    assert media_file.checksum == ""


def test_select_pub_media_file_prefers_highest_video_label() -> None:
    data = {
        "pubName": "Meeting Video",
        "files": {
            "T": {
                "MP4": [
                    {
                        "label": "360p",
                        "file": {"url": "https://example.test/video-360.mp4"},
                    },
                    {
                        "label": "720p",
                        "title": "Best Video",
                        "file": {"url": "https://example.test/video-720.mp4"},
                        "images": {"sm": {"url": "https://example.test/thumb.jpg"}},
                    },
                ]
            }
        },
    }

    media_file = select_pub_media_file(
        data,
        "T",
        VIDEO_FORMATS,
        prefer_highest_label=True,
    )

    assert media_file is not None
    assert media_file.url == "https://example.test/video-720.mp4"
    assert media_file.title == "Best Video"
    assert media_file.thumbnail_url == "https://example.test/thumb.jpg"
    assert media_file.label == "720p"


def test_build_pub_media_url_encodes_query_params() -> None:
    assert build_pub_media_url(
        {
            "pub": "mwb",
            "langwritten": "pt BR",
        },
        "https://example.test/api",
    ) == "https://example.test/api?pub=mwb&langwritten=pt+BR"


def test_resolve_publication_video_link_uses_publication_identifiers(monkeypatch) -> None:
    calls = []

    def fetch(params):
        calls.append(params)
        return {
            "files": {
                "T": {
                    "MP4": [
                        {
                            "title": "Resolved",
                            "file": {"url": "https://example.test/video.mp4"},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(publication_links, "fetch_pub_media_json", fetch)

    media_file = resolve_publication_video_link("mwb", 3, 202605, 0, "T")

    assert media_file is not None
    assert media_file.url == "https://example.test/video.mp4"
    assert media_file.title == "Resolved"
    assert calls == [
        {
            "pub": "mwb",
            "track": 3,
            "langwritten": "T",
            "fileformat": "mp4,m4v",
            "output": "json",
            "alllangs": "0",
            "issue": 202605,
        }
    ]


def test_resolve_publication_video_link_normalizes_sign_language_song(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        publication_links,
        "fetch_pub_media_json",
        lambda params: calls.append(params) or {},
    )

    media_file = resolve_publication_video_link(
        "sjjm",
        7,
        0,
        0,
        "T",
        is_sign_language=True,
    )

    assert media_file is None
    assert calls == [
        {
            "pub": "sjj",
            "track": 7,
            "langwritten": "T",
            "fileformat": "mp4,m4v",
            "output": "json",
            "alllangs": "0",
        }
    ]


def test_resolve_publication_video_link_falls_back_to_document_id(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        publication_links,
        "fetch_pub_media_json",
        lambda params: calls.append(params) or {},
    )

    media_file = resolve_publication_video_link("", 0, 0, 12345, "T")

    assert media_file is None
    assert calls == [
        {
            "docid": 12345,
            "langwritten": "T",
            "fileformat": "mp4,m4v",
            "output": "json",
            "alllangs": "0",
        }
    ]
