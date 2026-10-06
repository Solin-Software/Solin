from __future__ import annotations

import gzip
import hashlib
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from solin.core.media.downloader import (
    SongDownloader,
    _DownloadJob,
)
from solin.core.media.download_storage import (
    DownloadTarget,
    DownloadProgressGate,
    cached_path_for,
    cleanup_incomplete_cache,
    commit_persistent_download,
    completed_cached_path,
    is_url_cached,
    make_persistent_temp_path,
    prepare_download_target,
    safe_remove,
)
from tests._paths import REPO_ROOT
from tests._http import LoopbackHTTPServer


def _download_from_server(
    tmp_path: Path,
    *,
    body: bytes,
    content_length: int,
    content_encoding: str = "",
) -> tuple[list[str], list[str]]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(content_length))
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, _format, *_args):
            return

    server = LoopbackHTTPServer(Handler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{server.server_port}/media.mp3"
    downloader = SongDownloader(tmp_path)
    job = _DownloadJob(
        job_id=1,
        url=url,
        persist=True,
        cancel_event=threading.Event(),
    )
    downloader._job = job
    finished: list[str] = []
    errors: list[str] = []
    downloader.finished.connect(finished.append)
    downloader.error.connect(errors.append)

    try:
        downloader._worker(job)
    finally:
        server.server_close()
        server_thread.join(timeout=5)

    return finished, errors


def test_downloader_accepts_transparently_decoded_response(monkeypatch, tmp_path):
    def unexpected_hostname_lookup(*_args):
        raise AssertionError("Loopback HTTP validation must not discover network hosts")

    monkeypatch.setattr(socket, "getfqdn", unexpected_hostname_lookup)
    decoded_body = b"media payload" * 1024
    encoded_body = gzip.compress(decoded_body)

    finished, errors = _download_from_server(
        tmp_path,
        body=encoded_body,
        content_length=len(encoded_body),
        content_encoding="gzip",
    )

    assert errors == []
    assert len(finished) == 1
    cached_file = Path(finished[0])
    assert cached_file.parent == tmp_path
    assert cached_file.suffix == ".mp3"
    assert len(cached_file.stem) == 64
    assert cached_file.read_bytes() == decoded_body
    assert Path(f"{cached_file}.done").is_file()


def test_downloader_rejects_truncated_encoded_transfer(tmp_path):
    body = b"partial payload"

    finished, errors = _download_from_server(
        tmp_path,
        body=body,
        content_length=len(body) * 2,
    )

    assert finished == []
    assert errors
    assert list(tmp_path.glob("*.mp3")) == []
    assert list(tmp_path.glob("*.done")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_cache_path_uses_exact_url_hash_and_safe_extension(tmp_path):
    first_url = "https://one.example/media/clip.MP4?quality=720"
    second_url = "https://two.example/media/clip.MP4?quality=720"

    first = Path(cached_path_for(first_url, tmp_path))
    second = Path(cached_path_for(second_url, tmp_path))

    assert first.name == f"{hashlib.sha256(first_url.encode()).hexdigest()}.mp4"
    assert second.name == f"{hashlib.sha256(second_url.encode()).hexdigest()}.mp4"
    assert first != second

    unsafe = Path(cached_path_for("https://example.test/media/file.bad-ext!", tmp_path))
    assert unsafe.name == hashlib.sha256(
        b"https://example.test/media/file.bad-ext!"
    ).hexdigest()


def test_cache_marker_must_match_url_exactly(tmp_path):
    url = "https://example.test/media/clip.mp4?token=one"
    path = Path(cached_path_for(url, tmp_path))
    path.write_bytes(b"wrong cache")
    Path(f"{path}.done").write_text(f"{url}\n", encoding="utf-8")

    assert completed_cached_path(url, tmp_path) is None
    assert is_url_cached(url, tmp_path) is False

    target = prepare_download_target(url, tmp_path, persist=True)
    assert target.is_cached is False
    assert target.final_path == str(path)
    safe_remove(target.write_path)


def test_valid_legacy_cache_entry_is_migrated_once(tmp_path):
    url = "https://example.test/media/legacy.mp3?download=1"
    legacy = tmp_path / "legacy.mp3"
    legacy.write_bytes(b"legacy media")
    Path(f"{legacy}.done").write_text(url, encoding="utf-8")
    destination = Path(cached_path_for(url, tmp_path))

    assert completed_cached_path(url, tmp_path) == str(destination)
    assert destination.read_bytes() == b"legacy media"
    assert Path(f"{destination}.done").read_text(encoding="utf-8") == url
    assert not legacy.exists()
    assert not Path(f"{legacy}.done").exists()
    assert completed_cached_path(url, tmp_path) == str(destination)


def test_legacy_cache_entry_with_different_marker_is_not_migrated(tmp_path):
    url = "https://example.test/media/legacy.mp3?download=1"
    legacy = tmp_path / "legacy.mp3"
    legacy.write_bytes(b"other media")
    Path(f"{legacy}.done").write_text(
        "https://other.example/media/legacy.mp3",
        encoding="utf-8",
    )
    destination = Path(cached_path_for(url, tmp_path))

    assert completed_cached_path(url, tmp_path) is None
    assert legacy.exists()
    assert not destination.exists()


def test_persistent_commit_publishes_exact_marker_and_no_staging_file(tmp_path):
    url = "https://example.test/media/clip.mp4"
    target = prepare_download_target(url, tmp_path, persist=True)
    Path(target.write_path).write_bytes(b"complete media")

    commit_persistent_download(target, url)

    assert Path(target.final_path).read_bytes() == b"complete media"
    assert Path(f"{target.final_path}.done").read_text(encoding="utf-8") == url
    assert completed_cached_path(url, tmp_path) == target.final_path
    assert not Path(target.write_path).exists()
    assert list(tmp_path.glob(".*.tmp")) == []
    assert list(tmp_path.glob("*.lock")) == []


def test_failed_marker_commit_never_exposes_entry_as_complete(monkeypatch, tmp_path):
    from solin.core.media import download_storage

    url = "https://example.test/media/clip.mp4"
    final_path = cached_path_for(url, tmp_path)
    write_path = make_persistent_temp_path(final_path)
    Path(write_path).write_bytes(b"new media")
    Path(f"{final_path}.done").write_text(url, encoding="utf-8")
    target = DownloadTarget(final_path, write_path, persist=True)

    def fail_marker_commit(*_args):
        raise OSError("marker failed")

    monkeypatch.setattr(
        download_storage,
        "_write_marker_atomically",
        fail_marker_commit,
    )

    with pytest.raises(OSError, match="marker failed"):
        commit_persistent_download(target, url)

    assert Path(final_path).read_bytes() == b"new media"
    assert not Path(f"{final_path}.done").exists()
    assert completed_cached_path(url, tmp_path) is None


def test_persistent_download_jobs_use_isolated_staging_files(tmp_path):
    final_path = str(tmp_path / "media.mp3")

    first = make_persistent_temp_path(final_path)
    second = make_persistent_temp_path(final_path)

    assert first != second
    assert os.path.dirname(first) == str(tmp_path)
    assert first.endswith(".tmp")
    assert second.endswith(".tmp")

    safe_remove(first)

    assert not os.path.exists(first)
    assert os.path.exists(second)
    safe_remove(second)


def test_cleanup_incomplete_cache_removes_only_unfinished_downloads(tmp_path):
    complete = tmp_path / "complete.mp3"
    complete.write_bytes(b"complete")
    complete.with_name("complete.mp3.done").write_text("url", encoding="utf-8")
    incomplete = tmp_path / "incomplete.mp3"
    incomplete.write_bytes(b"incomplete")
    staging = tmp_path / ".media.mp3.abc.tmp"
    staging.write_bytes(b"staging")
    marker = tmp_path / "orphan.done"
    marker.write_text("marker", encoding="utf-8")

    removed = cleanup_incomplete_cache(tmp_path)

    assert removed == 2
    assert complete.exists()
    assert complete.with_name("complete.mp3.done").exists()
    assert marker.exists()
    assert not incomplete.exists()
    assert not staging.exists()


def test_cleanup_incomplete_cache_preserves_active_download(tmp_path):
    target = prepare_download_target(
        "https://example.test/media/active.mp4",
        tmp_path,
        persist=True,
    )
    staging = Path(target.write_path)
    staging.write_bytes(b"partial")

    removed = cleanup_incomplete_cache(tmp_path)

    assert removed == 0
    assert staging.exists()
    assert Path(f"{staging}.lock").exists()
    safe_remove(staging)


def test_completed_cached_path_ignores_local_sources(tmp_path):
    local_media = tmp_path / "local-video.mp4"
    local_media.write_bytes(b"local")

    assert completed_cached_path(str(local_media), tmp_path / "cache") is None
    assert not (tmp_path / "cache").exists()


def test_download_progress_gate_throttles_by_percent_byte_and_time():
    now = 1.0

    def clock() -> float:
        return now

    gate = DownloadProgressGate(clock=clock)

    assert gate.should_emit(1, 1000)
    assert not gate.should_emit(2, 1000)
    assert gate.should_emit(10, 1000)

    unknown_total = DownloadProgressGate(clock=clock)
    assert unknown_total.should_emit(1, 0)
    assert not unknown_total.should_emit(2, 0)

    now = 1.21
    assert unknown_total.should_emit(2, 0)


def test_media_download_storage_has_no_qt_or_downloader_dependency():
    source = (
        REPO_ROOT
        / "src"
        / "solin"
        / "core"
        / "media"
        / "download_storage.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "QObject" not in source
    assert "Signal" not in source
    assert "SongDownloader" not in source
