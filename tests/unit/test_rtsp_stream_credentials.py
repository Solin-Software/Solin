"""RTSP stream credentials reach the camera without being stored in the address.

ffmpeg_source takes a single `input` string and the RTSP demuxer exposes no
user/password option, so a login can only reach the camera embedded in the URI.
Solin keeps it out of the stored address and re-attaches it when the source is
created, which means the secret must travel beside the document, never inside it.
"""

from __future__ import annotations

from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph


def _graph(credentials: dict) -> LibobsSceneGraph:
    graph = LibobsSceneGraph.__new__(LibobsSceneGraph)
    graph._source_credentials = credentials
    return graph


def test_the_login_is_spliced_into_the_uri() -> None:
    graph = _graph({"cam": {"username": "admin", "password": "hunter2"}})

    assert graph._authenticated_uri("rtsp://10.0.0.5:554/stream", "cam") == (
        "rtsp://admin:hunter2@10.0.0.5:554/stream"
    )


def test_special_characters_are_percent_encoded() -> None:
    """libavformat decodes the userinfo, so an unencoded @ or / splits the URI."""
    graph = _graph({"cam": {"username": "ad/min", "password": "p@ss:w?rd"}})

    spliced = graph._authenticated_uri("rtsp://10.0.0.5:554/s", "cam")

    assert spliced == "rtsp://ad%2Fmin:p%40ss%3Aw%3Frd@10.0.0.5:554/s"


def test_a_vendor_path_login_is_left_untouched() -> None:
    """Some firmware wants the login in the path; that form must still work."""
    uri = (
        "rtsp://10.0.0.5:554/user=admin_password=FrwI7MsQ"
        "_channel=0_stream=0&protocol=unicast.sdp?real_stream"
    )

    assert _graph({})._authenticated_uri(uri, "cam") == uri


def test_an_address_that_already_carries_a_login_is_not_doubled() -> None:
    graph = _graph({"cam": {"username": "admin", "password": "hunter2"}})
    uri = "rtsp://someone:else@10.0.0.5:554/s"

    assert graph._authenticated_uri(uri, "cam") == uri


def test_ipv6_hosts_keep_their_brackets() -> None:
    graph = _graph({"cam": {"username": "a", "password": "b"}})

    assert graph._authenticated_uri("rtsp://[fe80::1]:554/s", "cam") == (
        "rtsp://a:b@[fe80::1]:554/s"
    )


def test_a_source_without_credentials_is_unchanged() -> None:
    graph = _graph({"other": {"username": "a", "password": "b"}})

    assert graph._authenticated_uri("rtsp://10.0.0.5/s", "cam") == "rtsp://10.0.0.5/s"


def test_blank_credentials_do_not_produce_an_empty_userinfo() -> None:
    graph = _graph({"cam": {"username": "", "password": ""}})

    assert graph._authenticated_uri("rtsp://10.0.0.5/s", "cam") == "rtsp://10.0.0.5/s"


# ── the secret must travel beside the document, never inside it ──────────────


class _Document:
    def __init__(self, sources) -> None:
        self.sources = sources


class _Source:
    def __init__(self, source_id: str, credential_ref: str) -> None:
        self.id = source_id
        self.credential_ref = credential_ref


def _client():
    from solin.core.scenes.process_engine import SubprocessSceneEngine

    client = SubprocessSceneEngine.__new__(SubprocessSceneEngine)
    client._credential_resolver = None
    return client


def test_references_are_resolved_for_the_wire() -> None:
    client = _client()
    client.set_credential_resolver(
        lambda ref: ("admin", "hunter2") if ref == "ptz-abc" else None
    )
    document = _Document([_Source("cam", "ptz-abc"), _Source("other", "")])

    record = client._source_credentials_record(document)

    assert record == {"cam": {"username": "admin", "password": "hunter2"}}


def test_no_resolver_sends_nothing() -> None:
    document = _Document([_Source("cam", "ptz-abc")])

    assert _client()._source_credentials_record(document) == {}


def test_an_unresolvable_reference_is_skipped_not_fatal() -> None:
    """A camera whose secret is gone must not stop the whole graph hydrating."""
    client = _client()
    client.set_credential_resolver(lambda ref: None)
    document = _Document([_Source("cam", "ptz-gone")])

    assert client._source_credentials_record(document) == {}


def test_a_failing_vault_does_not_break_hydrate() -> None:
    client = _client()

    def explode(_ref):
        raise RuntimeError("keyring unavailable")

    client.set_credential_resolver(explode)
    document = _Document([_Source("cam", "ptz-abc")])

    assert client._source_credentials_record(document) == {}


def test_the_document_record_never_carries_the_secret() -> None:
    """It is persisted to disk and kept in a long-lived signature cache."""
    from solin.core.scenes.engine import scene_engine_document_record
    from solin.core.scenes.presets import SceneSeedNames, create_default_scene_document

    document = create_default_scene_document(
        SceneSeedNames(
            content_source="c", default_camera_source="d", no_signal_source="n",
            content_scene="Content", camera_scene="Camera",
            content_camera_pip_scene="pip", no_signal_scene="ns",
            content_layer="cl", camera_layer="caml", background_layer="bg",
        ),
        document_id="doc",
        created_at="2026-09-08T12:00:00+00:00",
    )

    record = scene_engine_document_record(document)

    assert "password" not in repr(record)
    assert "source_credentials" not in record
