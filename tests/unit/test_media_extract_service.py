"""Process ownership and validation of the media extraction service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
import time

import pytest

from solin.core.media.info_queue import MediaInfoFailureKind
from solin.core.media.media_extract import (
    ImagePolicy,
    MediaExtractCancelled,
    MediaExtractError,
    MediaExtractRequest,
    MediaExtractService,
    decode_response,
)


def _response(**fields):
    header = {
        "protocol": 1,
        "status": "ok",
        "title": "Canção 🎵",
        "duration_ms": 200,
        "has_video": True,
        "image": None,
        **fields,
    }
    return json.dumps(header, ensure_ascii=False).encode() + b"\n"


def _wait(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_unicode_metadata_has_no_locale_dependency():
    result = decode_response(_response(), MediaExtractRequest("canção.mp4"))
    assert result.title == "Canção 🎵"
    assert result.duration_ms == 200
    assert result.has_video


def test_owned_bgra_payload_preserves_pixel_identity():
    pixels = bytes((1, 2, 3, 255)) * 2
    result = decode_response(
        _response(image={"width": 2, "height": 1, "stride": 8, "bytes": 8, "kind": "cover"})
        + pixels,
        MediaExtractRequest("song.mp3", image_policy=ImagePolicy.COVER_ONLY),
    )
    assert result.image is not None
    assert result.image.data == pixels
    assert result.image.kind == "cover"


@pytest.mark.parametrize(
    "fields",
    [
        {"protocol": True},
        {"protocol": 2},
        {"status": "unknown"},
        {"duration_ms": True},
        {"duration_ms": -1},
        {"duration_ms": 2**63},
        {"has_video": 1},
        {"title": None},
        {"image": {"width": 0, "height": 1, "stride": 0, "bytes": 0, "kind": "cover"}},
        {"image": {"width": 2, "height": 1, "stride": 3, "bytes": 0, "kind": "cover"}},
        {"image": {"width": 640, "height": 361, "stride": 2560, "bytes": 0, "kind": "frame"}},
        {"image": {"width": 1, "height": 1, "stride": 4, "bytes": 4, "kind": "unknown"}},
    ],
)
def test_malformed_metadata_and_pixel_layout_are_rejected(fields):
    with pytest.raises(MediaExtractError) as caught:
        decode_response(_response(**fields), MediaExtractRequest("sample.mp4"))
    assert caught.value.failure.code == "extractor-protocol"


@pytest.mark.parametrize(
    "payload",
    [b"", b"{}", b"{\xff}\n", b"{" * 65537 + b"\n", _response() + b"unexpected pixels"],
    ids=["empty", "missing-newline", "invalid-utf8", "oversized-header", "trailing-pixels"],
)
def test_truncated_invalid_or_unclaimed_payload_is_rejected(payload):
    with pytest.raises(MediaExtractError):
        decode_response(payload, MediaExtractRequest("sample.mp4"))


def test_cover_only_does_not_accept_a_video_frame():
    with pytest.raises(MediaExtractError):
        decode_response(
            _response(image={"width": 1, "height": 1, "stride": 4, "bytes": 4, "kind": "frame"})
            + b"\0" * 4,
            MediaExtractRequest("song.mp3", image_policy=ImagePolicy.COVER_ONLY),
        )


def test_helper_failure_is_not_an_authoritative_empty_result():
    payload = (
        json.dumps(
            {
                "protocol": 1,
                "status": "error",
                "error": {
                    "kind": "transient",
                    "code": "http-503",
                    "message": "Origin unavailable",
                },
            }
        ).encode()
        + b"\n"
    )
    with pytest.raises(MediaExtractError) as caught:
        decode_response(payload, MediaExtractRequest("https://example/media.mp4"))
    assert caught.value.failure.kind is MediaInfoFailureKind.TRANSIENT
    assert caught.value.failure.code == "http-503"


def test_metadata_only_request_never_accepts_unrequested_pixels():
    request = MediaExtractRequest(
        "sample.mp4", image_policy=ImagePolicy.NONE, require_title=False, require_duration=False
    )
    result = decode_response(_response(), request)
    assert result.title == ""
    assert result.duration_ms == 0
    with pytest.raises(MediaExtractError):
        decode_response(_response(image={}) + b"\0", request)


def test_child_process_roundtrip_does_not_require_external_tools(monkeypatch):
    monkeypatch.setenv("PATH", "")
    script = "import sys; sys.stdout.buffer.write(" + repr(_response()) + "); sys.stdout.flush()"
    service = MediaExtractService(command=(sys.executable, "-c", script))
    try:
        result = service.extract(MediaExtractRequest("canção d'água.mp4"))
        assert result.title == "Canção 🎵"
        assert service.active_count == 0
    finally:
        service.close()


def test_native_child_cannot_inherit_library_overrides_but_keeps_network_policy(monkeypatch):
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "DYLD_INSERT_LIBRARIES"):
        monkeypatch.setenv(name, "unqualified-native-runtime")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "operator-certificate.pem")
    monkeypatch.setenv("PATH", "external-ffmpeg-bin")
    script = (
        "import json,os,sys; "
        "document=" + repr(json.loads(_response())) + "; "
        "document['title']=json.dumps(dict(os.environ)); "
        "sys.stdout.buffer.write(json.dumps(document).encode()+b'\\n')"
    )
    service = MediaExtractService(command=(sys.executable, "-c", script))
    try:
        environment = json.loads(service.extract(MediaExtractRequest("sample.mp4")).title)
    finally:
        service.close()
    assert not any(
        name in environment
        for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "DYLD_INSERT_LIBRARIES")
    )
    assert environment["HTTP_PROXY"] == "http://proxy.example:8080"
    assert environment["SSL_CERT_FILE"] == "operator-certificate.pem"
    assert "external-ffmpeg-bin" not in environment["PATH"]
    import os

    assert os.environ["PATH"] == "external-ffmpeg-bin"


def test_cancel_reaps_running_child_and_releases_slot():
    service = MediaExtractService(command=(sys.executable, "-c", "import time; time.sleep(60)"))
    cancel = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                service.extract, MediaExtractRequest("sample.mp4"), cancel=cancel
            )
            _wait(lambda: service.active_count == 1)
            cancel.set()
            with pytest.raises(MediaExtractCancelled):
                future.result(timeout=3)
        assert service.active_count == 0
    finally:
        service.close()


def test_global_limit_and_cancelled_waiter_do_not_start_extra_children():
    service = MediaExtractService(command=(sys.executable, "-c", "import time; time.sleep(60)"))
    cancellations = [threading.Event() for _ in range(3)]
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            running = [
                executor.submit(
                    service.extract, MediaExtractRequest(str(i)), cancel=cancellations[i]
                )
                for i in range(2)
            ]
            _wait(lambda: service.active_count == 2)
            waiting = executor.submit(
                service.extract, MediaExtractRequest("queued"), cancel=cancellations[2]
            )
            cancellations[2].set()
            with pytest.raises(MediaExtractCancelled):
                waiting.result(timeout=3)
            assert service.active_count == 2
            service.close()
            for future in running:
                with pytest.raises(MediaExtractCancelled):
                    future.result(timeout=3)
        assert service.active_count == 0
    finally:
        service.close()


def test_timeout_reaps_child_and_preserves_transient_retry_classification():
    service = MediaExtractService(
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        timeout=0.2,
    )
    try:
        with pytest.raises(MediaExtractError) as caught:
            service.extract(MediaExtractRequest("sample.mp4"))
        assert caught.value.failure.code == "extractor-timeout"
        assert caught.value.failure.kind is MediaInfoFailureKind.TRANSIENT
        assert service.active_count == 0
    finally:
        service.close()


def test_unexpected_native_exit_is_a_failure_not_missing_cover():
    service = MediaExtractService(command=(sys.executable, "-c", "raise SystemExit(7)"))
    try:
        with pytest.raises(MediaExtractError) as caught:
            service.extract(MediaExtractRequest("song.mp3", image_policy=ImagePolicy.COVER_ONLY))
        assert caught.value.failure.code == "extractor-exit"
        assert service.active_count == 0
    finally:
        service.close()


def test_oversized_child_output_is_bounded_and_reaped():
    script = "import sys,time; sys.stdout.buffer.write(b'x' * 1100000); sys.stdout.flush(); time.sleep(60)"
    service = MediaExtractService(command=(sys.executable, "-c", script))
    try:
        with pytest.raises(MediaExtractError) as caught:
            service.extract(MediaExtractRequest("sample.mp4"))
        assert caught.value.failure.code == "extractor-protocol"
        assert service.active_count == 0
    finally:
        service.close()


def test_late_default_service_callback_cannot_restart_children_after_shutdown(monkeypatch):
    from solin.core.media import media_extract

    service = MediaExtractService(command=(sys.executable, "-c", "raise SystemExit(7)"))
    monkeypatch.setattr(media_extract, "_default_service", service)
    media_extract.shutdown_media_extract_service()
    assert media_extract.get_media_extract_service() is service
    with pytest.raises(MediaExtractCancelled):
        service.extract(MediaExtractRequest("late.mp4"))
    assert service.active_count == 0


def test_bootstrap_can_begin_a_new_application_lifetime_after_shutdown(monkeypatch):
    from solin.core.media import media_extract

    previous = MediaExtractService()
    previous.close()
    monkeypatch.setattr(media_extract, "_default_service", previous)
    media_extract.initialize_media_extract_service()
    replacement = media_extract.get_media_extract_service()
    assert replacement is not previous
    replacement.close()


def test_fast_exit_cannot_bypass_diagnostic_output_limit():
    script = (
        "import sys; sys.stderr.buffer.write(b'x' * 80000); "
        "sys.stdout.buffer.write(" + repr(_response()) + ")"
    )
    service = MediaExtractService(command=(sys.executable, "-c", script))
    try:
        with pytest.raises(MediaExtractError) as caught:
            service.extract(MediaExtractRequest("sample.mp4"))
        assert caught.value.failure.code == "extractor-protocol"
        assert service.active_count == 0
    finally:
        service.close()


def test_reopening_waits_for_completion_of_previous_lifetime_shutdown(monkeypatch):
    from solin.core.media import media_extract

    draining, release, initializing = threading.Event(), threading.Event(), threading.Event()

    class DelayedDrain(MediaExtractService):
        def close(self):
            self._stop.set()
            draining.set()
            assert release.wait(3)
            super().close()

    previous = DelayedDrain()
    monkeypatch.setattr(media_extract, "_default_service", previous)

    def initialize():
        initializing.set()
        media_extract.initialize_media_extract_service()

    with ThreadPoolExecutor(max_workers=2) as executor:
        shutdown = executor.submit(media_extract.shutdown_media_extract_service)
        assert draining.wait(2)
        initialization = executor.submit(initialize)
        try:
            assert initializing.wait(2)
            with pytest.raises(TimeoutError):
                initialization.result(timeout=0.05)
            assert media_extract._default_service is previous
        finally:
            release.set()
        shutdown.result(timeout=3)
        initialization.result(timeout=3)
    assert media_extract.get_media_extract_service() is not previous
    media_extract.shutdown_media_extract_service()
