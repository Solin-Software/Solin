from __future__ import annotations

import gzip
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from solin.core.media.downloader import (
    SongDownloader,
    _DownloadJob,
)
from solin.core.media.download_storage import (
    DownloadProgressGate,
    cleanup_incomplete_cache,
    completed_cached_path,
    make_persistent_temp_path,
    safe_remove,
)
from tests._paths import REPO_ROOT


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

    server = HTTPServer(("127.0.0.1", 0), Handler)
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


def test_downloader_accepts_transparently_decoded_response(tmp_path):
    decoded_body = b"media payload" * 1024
    encoded_body = gzip.compress(decoded_body)

    finished, errors = _download_from_server(
        tmp_path,
        body=encoded_body,
        content_length=len(encoded_body),
        content_encoding="gzip",
    )

    cached_file = tmp_path / "media.mp3"
    assert errors == []
    assert finished == [str(cached_file)]
    assert cached_file.read_bytes() == decoded_body
    assert cached_file.with_name("media.mp3.done").is_file()


def test_downloader_rejects_truncated_encoded_transfer(tmp_path):
    body = b"partial payload"

    finished, errors = _download_from_server(
        tmp_path,
        body=body,
        content_length=len(body) * 2,
    )

    assert finished == []
    assert errors
    assert not (tmp_path / "media.mp3").exists()
    assert not (tmp_path / "media.mp3.done").exists()
    assert list(tmp_path.glob("*.tmp")) == []


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
