from solin.core.jw import publication_links
from solin.core.jw.publication_links import (
    JwpubMediaRequest,
    PublicationMediaRequest,
    PublicationMediaResolver,
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
                        "duration": 12.5,
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
    assert media_file.duration_ticks == 125_000_000


def test_select_pub_media_file_uses_nested_file_duration_fallback() -> None:
    data = {
        "files": {
            "T": {
                "MP4": [
                    {
                        "file": {
                            "url": "https://example.test/video.mp4",
                            "duration": "3.25",
                        }
                    }
                ]
            }
        }
    }

    media_file = select_pub_media_file(data, "T", VIDEO_FORMATS)

    assert media_file is not None
    assert media_file.duration_ticks == 32_500_000


def test_select_pub_media_file_falls_back_after_invalid_item_duration() -> None:
    data = {
        "files": {
            "T": {
                "MP4": [
                    {
                        "duration": 0,
                        "file": {
                            "url": "https://example.test/video.mp4",
                            "duration": 4.5,
                        },
                    }
                ]
            }
        }
    }

    media_file = select_pub_media_file(data, "T", VIDEO_FORMATS)

    assert media_file is not None
    assert media_file.duration_ticks == 45_000_000


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


def test_publication_media_resolver_resolves_video_request(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        publication_links,
        "fetch_pub_media_json",
        lambda params: calls.append(params)
        or {
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
        },
    )

    media_file = PublicationMediaResolver().resolve_video(
        PublicationMediaRequest(
            key_symbol="mwb",
            track=3,
            issue_tag=202605,
            meps_doc_id=0,
            language="T",
        )
    )

    assert media_file is not None
    assert media_file.url == "https://example.test/video.mp4"
    assert calls[0]["pub"] == "mwb"


def test_publication_media_resolver_returns_jwpub_info(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        publication_links,
        "fetch_pub_media_json",
        lambda params: calls.append(params)
        or {
            "files": {
                "T": {
                    "JWPUB": [
                        {
                            "file": {
                                "url": "https://example.test/mwb.jwpub",
                                "checksum": "checksum",
                            }
                        }
                    ]
                }
            }
        },
    )

    media_info = PublicationMediaResolver().resolve_jwpub(
        JwpubMediaRequest(pub="mwb", language="T", issue="202605")
    )

    assert media_info.download_url == "https://example.test/mwb.jwpub"
    assert media_info.checksum == "checksum"
    assert media_info.not_found is False
    assert calls == [
        {
            "pub": "mwb",
            "issue": "202605",
            "langwritten": "T",
            "fileformat": "JWPUB",
            "output": "json",
            "alllangs": "0",
            "txtCMSLang": "E",
        }
    ]
