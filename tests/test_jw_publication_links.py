from solin.core.jw.publication_links import (
    VIDEO_FORMATS,
    build_pub_media_url,
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
