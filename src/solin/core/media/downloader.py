"""
SongDownloader — download media in the background while the player streams the URL.

Operating modes
---------------
persist=True (default / automatic downloads ON):
    Save in MEDIA_CACHE_DIR/<filename> with a .done marker.
    Cached files are reused on the next playback.

persist=False (automatic downloads OFF):
    Save in an OS temporary file (e.g. /tmp/Solin_stream_XXXX.mp4).
    Create NO permanent files.
    The same progress signal drives the buffer bar.
    The same finished signal switches playback to the local file,
    protecting against CDN outages without a persistent cache.
    Delete the temporary file through cleanup_temp() / cancel().
"""
import os
import logging
import threading
from dataclasses import dataclass
from PySide6.QtCore import QObject, Signal

from solin.core.network.http import stream_get
from .download_storage import (
    DownloadProgressGate,
    commit_persistent_download,
    completed_cached_path,
    prepare_download_target,
    safe_remove,
)

log = logging.getLogger(__name__)


@dataclass
class _DownloadJob:
    job_id: int
    url: str
    persist: bool
    cancel_event: threading.Event
    writing_tmp: str | None = None


class SongDownloader(QObject):
    """
    Signals:
      progress(downloaded_bytes, total_bytes) - progress update
      finished(local_path)                    - download complete
      error(msg)                              - download failure
    """
    progress = Signal(int, int)
    finished = Signal(str)
    error    = Signal(str)

    _worker_progress = Signal(int, int, int)
    _worker_finished = Signal(int, str, bool)
    _worker_error = Signal(int, str)

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._lock = threading.Lock()
        self._next_job_id = 0
        self._job: _DownloadJob | None = None
        self._thread: threading.Thread | None = None
        self._finished_temp: str | None = None
        self._worker_progress.connect(self._deliver_progress)
        self._worker_finished.connect(self._deliver_finished)
        self._worker_error.connect(self._deliver_error)
        self.destroyed.connect(lambda *_: self.cancel())

    # -- API publica ----------------------------------------------------------

    def get_cached_path(self, url: str):
        """Return the local path if the persistent file already exists."""
        return completed_cached_path(url, self._media_cache_dir)

    def start(self, url: str, persist: bool = True) -> int:
        """
        Start a background download.
        persist=True → save permanently in MEDIA_CACHE_DIR.
        persist=False → save in a temporary file, deleted by cleanup_temp/cancel.
        """
        self.cancel()
        with self._lock:
            self._next_job_id += 1
            job = _DownloadJob(
                job_id=self._next_job_id,
                url=url,
                persist=persist,
                cancel_event=threading.Event(),
            )
            self._job = job
            thread = threading.Thread(
                target=self._worker,
                args=(job,),
                daemon=True,
                name=f"media-download-{job.job_id}",
            )
            self._thread = thread
        thread.start()
        return job.job_id

    def cancel(self) -> None:
        """Cancel the current job and invalidate all callbacks still queued."""
        with self._lock:
            job = self._job
            self._job = None
            self._thread = None
            if job is not None:
                job.cancel_event.set()
                tmp = job.writing_tmp
                job.writing_tmp = None
            else:
                tmp = None
        safe_remove(tmp)

    def cleanup_temp(self) -> None:
        """
        Delete the temporary file handed to the player (persist=False).
        MediaController must call this in stop() and before the next playback.
        """
        with self._lock:
            path = self._finished_temp
            self._finished_temp = None
        safe_remove(path)

    def take_finished_temp(self):
        """Return and clear the completed temporary file path for external tracking."""
        with self._lock:
            path = self._finished_temp
            self._finished_temp = None
        return path

    # -- Worker ---------------------------------------------------------------

    def _worker(self, job: _DownloadJob) -> None:
        url = job.url
        persist = job.persist
        target = prepare_download_target(
            url,
            self._media_cache_dir,
            persist=persist,
        )
        final_path = target.final_path
        write_tmp = target.write_path
        if target.is_cached:
            size = target.cached_size or 0
            try:
                self._worker_progress.emit(job.job_id, size, size)
                self._worker_finished.emit(job.job_id, final_path, True)
            except RuntimeError:
                return
            return

        with self._lock:
            job.writing_tmp = write_tmp

        succeeded = False
        try:
            if job.cancel_event.is_set():
                return
            with stream_get(url, timeout=30) as resp:
                total      = int(resp.headers.get("content-length", 0))
                downloaded = 0
                progress_gate = DownloadProgressGate()

                def emit_progress(force: bool = False) -> None:
                    if progress_gate.should_emit(
                        downloaded,
                        total,
                        force=force,
                        cancelled=job.cancel_event.is_set(),
                    ):
                        self._worker_progress.emit(job.job_id, downloaded, total)

                with open(write_tmp, "wb") as f:
                    for chunk in resp.iter_bytes(131_072):
                        if job.cancel_event.is_set():
                            return
                        f.write(chunk)
                        downloaded += len(chunk)
                        emit_progress()

            if job.cancel_event.is_set():
                return
            # The transport validates encoded transfer length before exposing chunks.
            emit_progress(force=True)

            if persist:
                with self._lock:
                    if (
                        self._job is not job
                        or job.cancel_event.is_set()
                    ):
                        return
                    commit_persistent_download(target, url)

            succeeded = True
            try:
                self._worker_finished.emit(job.job_id, final_path, persist)
            except RuntimeError:
                if not persist:
                    safe_remove(final_path)

        except Exception as exc:  # noqa: BLE001 - background download job boundary
            log.exception("Media download job %s failed", job.job_id)
            if not job.cancel_event.is_set():
                try:
                    self._worker_error.emit(job.job_id, str(exc))
                except RuntimeError:
                    log.debug("Downloader was destroyed before error delivery")
        finally:
            if not succeeded:
                with self._lock:
                    if job.writing_tmp == write_tmp:
                        job.writing_tmp = None
                safe_remove(write_tmp)

    def _is_current_job(self, job_id: int) -> bool:
        with self._lock:
            return self._job is not None and self._job.job_id == job_id

    def _deliver_progress(self, job_id: int, downloaded: int, total: int) -> None:
        if self._is_current_job(job_id):
            self.progress.emit(downloaded, total)

    def _deliver_finished(self, job_id: int, local_path: str, persist: bool) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                stale = True
            else:
                stale = False
                self._job.writing_tmp = None
                self._job = None
                self._thread = None
                if not persist:
                    self._finished_temp = local_path
        if stale:
            if not persist:
                safe_remove(local_path)
            return
        self.finished.emit(local_path)

    def _deliver_error(self, job_id: int, message: str) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                return
            self._job = None
            self._thread = None
        self.error.emit(message)
