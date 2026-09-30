"""
Qt media metadata extraction and thumbnail services.
Extração centralizada de thumbnail + título de mídia, de forma assíncrona
e sem download completo de arquivos remotos.

Fontes de metadado (em ordem de prioridade):
  Áudio local   : bytes brutos do header (ID3v2/MP4 atoms/FLAC/OGG) → sem player
  Áudio remoto  : HTTP Range request (512 KB máx) → mesmos parsers de bytes
  Vídeo local   : QMediaPlayer → QMediaMetaData (CoverArtImage + Title) → frame 5%
  Vídeo remoto  : QMediaPlayer em streaming → mesma lógica (sem download completo)
  URL qualquer  : RemotePageMetaExtractor → og:image + og:title via HTTP HEAD/GET parcial
  Fallback local: arquivo remoto completo com .done → usado após falha da origem

Sinal principal:  info_ready(index, pixmap, title)
  - pixmap : thumbnail extraída (QPixmap válido) ou QPixmap() se não encontrada
  - title  : título dos metadados, ou "" se não disponível

Para alimentação ao vivo (player em reprodução), use feed_live_frame() /
feed_live_cover() na ThumbnailQueue — emitem info_ready com title="".

Nota: completamente independente da API JW.org. Thumb e título são extraídos
diretamente do stream de mídia (QMediaPlayer) ou dos metadados HTML da página.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import zlib

import shiboken6
from pathlib import Path
from typing import Any, Callable, cast

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtGui import QPixmap, QImage

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.media import ffprobe_metadata
from ..core.foundation.thread_workers import CancellationFlag, WorkerHandle, WorkerPool
from ..core.media.info_queue import (
    MediaInfoFailure,
    MediaInfoFailureKind,
    MediaInfoJob,
    MediaInfoScheduler,
    MediaInfoVersion,
    retry_delay_seconds,
)
from ..core.media.download_storage import completed_cached_path
from ..core.media.local_source import source_signature_from_stat
from ..core.network.http import (
    HttpDecodeError,
    HttpError,
    HttpResponseTooLargeError,
    HttpStatusError,
    HttpTransportError,
    get as http_get,
    get_bytes,
)

log = logging.getLogger(__name__)

_DEFAULT_METADATA_READ_BYTES = 512 * 1024
_MAX_ID3_TAG_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MEDIA_INFO_CACHE_SCHEMA = 3
_SOURCE_CHANGE_RETRY_DELAYS_MS = (100, 300, 1_000)


@dataclass(slots=True)
class _MediaInfoRequestState:
    source_url: str
    source_identity: str
    media_type: str
    require_thumbnail: bool
    require_title: bool
    require_duration: bool
    restart_on_source_change: bool
    source_change_attempts: int = 0
    attempts: int = 0
    fallback_url: str = ""
    origin_failure: MediaInfoFailure | None = None
    embedded_image_failed: bool = False
    partial_pixmap: QPixmap | None = None
    partial_title: str = ""
    partial_duration_ms: int = 0
    duration_fallback_started: bool = False


@dataclass(frozen=True, slots=True)
class _DiskCacheLookup:
    version: MediaInfoVersion
    index: int
    url: str
    media_type: str
    require_thumbnail: bool
    require_title: bool
    require_duration: bool
    restart_on_source_change: bool


@dataclass(frozen=True, slots=True)
class _DiskCacheResult:
    hit: bool
    image: QImage | None = None
    title: str = ""
    title_resolved: bool = False
    duration_ms: int = 0
    source_identity: str = ""


@dataclass(frozen=True, slots=True)
class _PendingMediaInfoResult:
    job: MediaInfoJob
    pixmap: QPixmap
    title: str


def _media_info_cache_source_identity(url: str) -> str:
    """Return a content-aware cache identity; call only from a worker."""

    if url.startswith(("http://", "https://")):
        return f"remote:{url}"
    normalized = os.path.normcase(os.path.abspath(url))
    try:
        source_stat = os.stat(url)
    except OSError:
        return f"local:{normalized}:unavailable"
    return f"local:{normalized}:{source_signature_from_stat(source_stat)}"


def _media_info_cache_paths(
    thumb_cache_dir: str,
    url: str,
    *,
    source_identity: str | None = None,
) -> tuple[str, str]:
    identity = source_identity or _media_info_cache_source_identity(url)
    digest = hashlib.md5(identity.encode("utf-8")).hexdigest()
    base_path = os.path.join(thumb_cache_dir, "extracted", digest)
    return f"{base_path}.jpg", f"{base_path}.json"


def _load_media_info_disk_cache(
    thumb_cache_dir: str,
    url: str,
    *,
    load_thumbnail: bool,
) -> _DiskCacheResult:
    """Load and decode a cache entry entirely outside the Qt GUI thread."""

    source_identity = _media_info_cache_source_identity(url)
    img_path, meta_path = _media_info_cache_paths(
        thumb_cache_dir,
        url,
        source_identity=source_identity,
    )
    try:
        with open(meta_path, "r", encoding="utf-8") as cache_file:
            data = json.load(cache_file)
    except FileNotFoundError:
        return _DiskCacheResult(False, source_identity=source_identity)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        log.debug("Could not read media info cache %s: %s", meta_path, exc)
        return _DiskCacheResult(False, source_identity=source_identity)
    if not isinstance(data, dict):
        return _DiskCacheResult(False, source_identity=source_identity)

    schema = data.get("schema")
    outcome = data.get("outcome")
    if schema not in {2, _MEDIA_INFO_CACHE_SCHEMA}:
        if data.get("has_thumb") is not True:
            return _DiskCacheResult(False, source_identity=source_identity)
        outcome = "ready"
    if outcome not in {"ready", "absent"}:
        return _DiskCacheResult(False, source_identity=source_identity)

    title = data.get("title", "")
    if not isinstance(title, str):
        title = ""
    title_resolved = (
        data.get("title_resolved") is True
        if schema == _MEDIA_INFO_CACHE_SCHEMA
        else bool(title)
    )
    raw_duration = data.get("duration_ms", 0)
    duration_ms = (
        max(0, raw_duration)
        if isinstance(raw_duration, int) and not isinstance(raw_duration, bool)
        else 0
    )

    image = QImage()
    if outcome == "ready" and load_thumbnail:
        image = QImage(img_path)
        if image.isNull():
            return _DiskCacheResult(False, source_identity=source_identity)
    current_source_identity = _media_info_cache_source_identity(url)
    if current_source_identity != source_identity:
        return _DiskCacheResult(False, source_identity=current_source_identity)
    return _DiskCacheResult(
        True,
        image,
        title,
        title_resolved,
        duration_ms,
        source_identity,
    )


def _save_media_info_disk_cache(
    thumb_cache_dir: str,
    url: str,
    image: QImage,
    title: str,
    *,
    title_resolved: bool,
    duration_ms: int,
    expected_source_identity: str | None = None,
) -> bool:
    """Persist an extraction result atomically from a background worker."""

    if expected_source_identity == "":
        return False
    source_identity = (
        expected_source_identity
        if expected_source_identity is not None
        else _media_info_cache_source_identity(url)
    )
    if _media_info_cache_source_identity(url) != source_identity:
        return False
    img_path, meta_path = _media_info_cache_paths(
        thumb_cache_dir,
        url,
        source_identity=source_identity,
    )
    temp_paths: list[str] = []
    try:
        cache_dir = os.path.dirname(img_path)
        os.makedirs(cache_dir, exist_ok=True)
        has_thumb = image is not None and not image.isNull()
        if has_thumb:
            image_fd, temp_image_path = tempfile.mkstemp(
                prefix="media-info-",
                suffix=".jpg",
                dir=cache_dir,
            )
            os.close(image_fd)
            temp_paths.append(temp_image_path)
            if not image.save(temp_image_path, quality=90):
                raise OSError("Qt could not encode the media info thumbnail")
        meta_fd, temp_meta_path = tempfile.mkstemp(
            prefix="media-info-",
            suffix=".json.tmp",
            dir=cache_dir,
            text=True,
        )
        os.close(meta_fd)
        temp_paths.append(temp_meta_path)
        with open(temp_meta_path, "w", encoding="utf-8") as cache_file:
            json.dump(
                {
                    "schema": _MEDIA_INFO_CACHE_SCHEMA,
                    "outcome": "ready" if has_thumb else "absent",
                    "title": title,
                    "title_resolved": title_resolved,
                    "has_thumb": has_thumb,
                    "duration_ms": max(0, int(duration_ms)),
                },
                cache_file,
                ensure_ascii=False,
            )
        if _media_info_cache_source_identity(url) != source_identity:
            return False
        if has_thumb:
            os.replace(temp_image_path, img_path)
            temp_paths.remove(temp_image_path)
        else:
            try:
                os.remove(img_path)
            except FileNotFoundError:
                pass
        os.replace(temp_meta_path, meta_path)
        temp_paths.remove(temp_meta_path)
        return True
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        log.debug("Could not save media info cache for %s: %s", url, exc)
        return False
    finally:
        for temp_path in temp_paths:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def _failure_from_exception(exc: BaseException) -> MediaInfoFailure:
    if isinstance(exc, HttpStatusError):
        status = exc.status_code
        transient = status in {408, 409, 423, 425, 429} or status >= 500
        return MediaInfoFailure(
            (
                MediaInfoFailureKind.TRANSIENT
                if transient
                else MediaInfoFailureKind.PERMANENT
            ),
            str(exc),
            f"http-{status}",
        )
    if isinstance(exc, (HttpDecodeError, HttpResponseTooLargeError, ValueError)):
        return MediaInfoFailure(
            MediaInfoFailureKind.FORMAT,
            str(exc),
            type(exc).__name__,
        )
    if isinstance(exc, (HttpTransportError, OSError)):
        return MediaInfoFailure(
            MediaInfoFailureKind.TRANSIENT,
            str(exc),
            type(exc).__name__,
        )
    return MediaInfoFailure(
        MediaInfoFailureKind.TRANSIENT,
        str(exc),
        type(exc).__name__,
    )

def _id3_tag_total_size(header: bytes) -> int | None:
    """Return the complete ID3v2 tag size, including its 10-byte header."""
    if len(header) < 10 or header[:3] != b"ID3":
        return None
    size_bytes = header[6:10]
    if any(value & 0x80 for value in size_bytes):
        return None
    payload_size = (
        (size_bytes[0] << 21)
        | (size_bytes[1] << 14)
        | (size_bytes[2] << 7)
        | size_bytes[3]
    )
    return 10 + payload_size


def _embedded_image_is_complete(data: bytes) -> bool:
    """Reject truncated common image payloads before invoking native decoders."""
    if data.startswith(_PNG_SIGNATURE):
        offset = len(_PNG_SIGNATURE)
        saw_header = False
        while offset + 12 <= len(data):
            chunk_size = struct.unpack(">I", data[offset : offset + 4])[0]
            chunk_type = data[offset + 4 : offset + 8]
            chunk_end = offset + 12 + chunk_size
            if chunk_end > len(data):
                return False
            chunk_data = data[offset + 8 : offset + 8 + chunk_size]
            expected_crc = struct.unpack(
                ">I", data[offset + 8 + chunk_size : chunk_end]
            )[0]
            if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
                return False
            if not saw_header:
                if chunk_type != b"IHDR" or chunk_size != 13:
                    return False
                saw_header = True
            offset = chunk_end
            if chunk_type == b"IEND":
                return chunk_size == 0
        return False

    if data.startswith(b"\xff\xd8\xff"):
        return data.rfind(b"\xff\xd9") >= 3
    if data.startswith((b"GIF87a", b"GIF89a")):
        return data.rfind(b"\x3b") >= 6
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return len(data) >= 12 and struct.unpack("<I", data[4:8])[0] + 8 <= len(data)
    return bool(data)


# ─────────────────────────────────────────────────────────────────────────────
# Parsers de metadado (operam em bytes — zero I/O de arquivo, zero player)
# ─────────────────────────────────────────────────────────────────────────────

def _audio_info_from_bytes(data: bytes, ext: str) -> "tuple[bytes | None, str]":
    """
    Extrai (cover_bytes, title) de dados de áudio em memória.
    Faz uma única passagem pelo header — cover e título lidos juntos.
    Retorna (None, "") se nada encontrado.
    """
    try:
        if ext == ".mp3":
            return _id3v2_info_from_bytes(data)
        if ext in (".m4a", ".aac", ".mp4", ".m4v"):
            return _mp4_info_from_bytes(data)
        if ext == ".flac":
            return _flac_info_from_bytes(data)
        if ext in (".ogg", ".opus"):
            return _ogg_info_from_bytes(data)
    except (IndexError, TypeError, UnicodeError, ValueError, struct.error):
        log_ignored_exception(__name__, "Could not parse audio metadata from bytes")
    return None, ""


def _read_audio_info_from_file(path: str) -> "tuple[bytes | None, str]":
    """Read a bounded metadata prefix, preserving I/O failures for callers."""

    ext = Path(path).suffix.lower()
    with open(path, "rb") as f:
        header = f.read(10)
        read_size = _DEFAULT_METADATA_READ_BYTES
        if ext == ".mp3":
            tag_size = _id3_tag_total_size(header)
            if tag_size is not None and tag_size <= _MAX_ID3_TAG_BYTES:
                read_size = max(read_size, tag_size)
            elif tag_size is not None:
                log.warning("Ignoring oversized ID3 tag in %s: %d bytes", path, tag_size)
        f.seek(0)
        data = f.read(read_size)
    return _audio_info_from_bytes(data, ext)


def _audio_info_from_file(path: str) -> "tuple[bytes | None, str]":
    """Best-effort compatibility wrapper around the bounded metadata reader."""

    try:
        return _read_audio_info_from_file(path)
    except (OSError, IndexError, TypeError, UnicodeError, ValueError, struct.error):
        log_ignored_exception(__name__, "Could not parse audio metadata from file")
    return None, ""


# ── ID3v2 (MP3) ───────────────────────────────────────────────────────────────

def _id3v2_info_from_bytes(data: bytes) -> "tuple[bytes | None, str]":
    """Extrai APIC (cover) e TIT2 (title) de tag ID3v2 v2.2/v2.3/v2.4."""
    cover: bytes | None = None
    title: str = ""
    try:
        hdr = data[:10]
        if hdr[:3] != b"ID3":
            return None, ""
        ver = hdr[3]
        sz = (
            (hdr[6] & 0x7F) << 21 |
            (hdr[7] & 0x7F) << 14 |
            (hdr[8] & 0x7F) <<  7 |
            (hdr[9] & 0x7F)
        )
        tag = data[10 : 10 + sz]
        pos = 0
        while pos < len(tag) and not (cover and title):
            if ver >= 4:
                # v2.4 usa Sync-Safe Integer para o tamanho do frame
                if pos + 10 > len(tag):
                    break
                fid = tag[pos : pos + 4].decode("latin-1", errors="ignore")
                fsz = (
                    (tag[pos + 4] << 21)
                    | (tag[pos + 5] << 14)
                    | (tag[pos + 6] << 7)
                    | tag[pos + 7]
                )
                frame_header_size = 10
            elif ver == 3:
                # v2.3 usa Integer normal de 32-bits
                if pos + 10 > len(tag):
                    break
                fid = tag[pos : pos + 4].decode("latin-1", errors="ignore")
                fsz = struct.unpack(">I", tag[pos + 4 : pos + 8])[0]
                frame_header_size = 10
            else:
                # v2.2 usa 24-bits
                if pos + 6 > len(tag):
                    break
                fid = tag[pos : pos + 3].decode("latin-1", errors="ignore")
                fsz = struct.unpack(">I", b"\x00" + tag[pos + 3 : pos + 6])[0]
                frame_header_size = 6

            if not fid.strip("\x00") or fsz <= 0:
                break
            frame_end = pos + frame_header_size + fsz
            if frame_end > len(tag):
                break
            fdat = tag[pos + frame_header_size : frame_end]
            pos = frame_end

            # Cover art
            if not cover and fid in ("APIC", "PIC") and fdat:
                enc = fdat[0]
                i = 1
                
                # 1. Pula o MIME type (sempre Latin-1, termina em único \x00)
                while i < len(fdat) and fdat[i] != 0:
                    i += 1
                i += 1  # Pula o \x00 do MIME
                
                # 2. Pula o Picture Type (1 byte)
                i += 1
                
                # 3. Pula a Description (depende do text encoding)
                if enc in (1, 2):  # UTF-16 (termina com duplo \x00\x00)
                    # Itera de 2 em 2 para não tropeçar no meio de um caractere
                    while i < len(fdat) - 1:
                        if fdat[i] == 0 and fdat[i+1] == 0:
                            i += 2
                            break
                        i += 2
                else:  # UTF-8 ou Latin-1 (termina com \x00)
                    while i < len(fdat) and fdat[i] != 0:
                        i += 1
                    i += 1
                    
                cover_raw = fdat[i:]
                
                # 4. FALLBACK 100% À PROVA DE BALAS (MAGIC BYTES)
                # Procura a assinatura exata para ignorar qualquer lixo residual do ID3
                if cover_raw:
                    jpg_idx = cover_raw.find(b'\xff\xd8\xff')
                    png_idx = cover_raw.find(_PNG_SIGNATURE)
                    gif_idx = cover_raw.find(b'GIF8')
                    
                    starts = [idx for idx in (jpg_idx, png_idx, gif_idx) if idx != -1]
                    if starts:
                        # Corta exatamente onde a imagem verdadeira começa
                        cover = cover_raw[min(starts):]
                    else:
                        # Se for um formato bizarro/desconhecido, tenta a sorte
                        cover = cover_raw or None

            # Title
            if not title and fid in ("TIT2", "TT2") and fdat:
                enc  = fdat[0] if fdat else 0
                raw  = fdat[1:]
                try:
                    if enc == 1:
                        title = raw.decode("utf-16", errors="ignore")
                    elif enc == 2:
                        title = raw.decode("utf-16-be", errors="ignore")
                    elif enc == 3:
                        title = raw.decode("utf-8", errors="ignore")
                    else:
                        title = raw.decode("latin-1", errors="ignore")
                    title = title.strip("\x00").strip()
                except (LookupError, UnicodeError, ValueError):
                    log_ignored_exception(__name__, "Could not decode ID3 title frame")
                    title = ""
    except (IndexError, TypeError, UnicodeError, ValueError, struct.error):
        log_ignored_exception(__name__, "Could not parse ID3 metadata")
    return cover, title


# ── MP4 / M4A / AAC ──────────────────────────────────────────────────────────

def _mp4_info_from_bytes(data: bytes) -> "tuple[bytes | None, str]":
    """Extrai 'covr' (cover) e '©nam' (title) de MP4/M4A/AAC."""
    cover: bytes | None = None
    title: str = ""
    try:
        # Cover — atom 'covr'
        idx = data.find(b"covr")
        if idx != -1:
            pos = idx + 4
            if pos + 16 <= len(data):
                data_sz = struct.unpack(">I", data[pos:pos+4])[0]
                if data[pos+4:pos+8] == b"data":
                    cover = data[pos+16: pos+data_sz] or None

        # Title — atom '\xa9nam'
        idx = data.find(b"\xa9nam")
        if idx != -1:
            pos = idx + 4
            if pos + 8 <= len(data):
                atom_sz = struct.unpack(">I", data[pos:pos+4])[0]
                if data[pos+4:pos+8] == b"data" and pos + 16 <= len(data):
                    raw = data[pos+16: pos+atom_sz]
                    title = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
    except (IndexError, TypeError, UnicodeError, ValueError, struct.error):
        log_ignored_exception(__name__, "Could not parse MP4 metadata")
    return cover, title


# ── FLAC ─────────────────────────────────────────────────────────────────────

def _flac_info_from_bytes(data: bytes) -> "tuple[bytes | None, str]":
    """Extrai PICTURE block (cover) e TITLE Vorbis comment de FLAC."""
    cover: bytes | None = None
    title: str = ""
    try:
        if data[:4] != b"fLaC":
            return None, ""
        pos = 4
        while pos < len(data) - 4 and not (cover and title):
            bh      = data[pos:pos+4]
            btype   = bh[0] & 0x7F
            is_last = (bh[0] & 0x80) != 0
            bsz     = struct.unpack(">I", b"\x00" + bh[1:4])[0]
            pos    += 4
            bdata   = data[pos: pos+bsz]
            pos    += bsz

            if btype == 6 and not cover:            # PICTURE block
                p = 4
                ml = struct.unpack(">I", bdata[p:p+4])[0]; p += 4 + ml
                dl = struct.unpack(">I", bdata[p:p+4])[0]; p += 4 + dl
                p += 16
                imglen = struct.unpack(">I", bdata[p:p+4])[0]; p += 4
                cover = bdata[p: p+imglen] or None

            if btype == 4 and not title:            # VORBIS_COMMENT block
                # vendor string
                vl = struct.unpack("<I", bdata[0:4])[0]
                p  = 4 + vl
                nc = struct.unpack("<I", bdata[p:p+4])[0]; p += 4
                for _ in range(nc):
                    cl = struct.unpack("<I", bdata[p:p+4])[0]; p += 4
                    comment = bdata[p:p+cl].decode("utf-8", errors="ignore"); p += cl
                    if comment.upper().startswith("TITLE="):
                        title = comment[6:].strip()
                        break

            if is_last:
                break
    except (IndexError, TypeError, UnicodeError, ValueError, struct.error):
        log_ignored_exception(__name__, "Could not parse FLAC metadata")
    return cover, title


# ── OGG / Opus ───────────────────────────────────────────────────────────────

def _ogg_info_from_bytes(data: bytes) -> "tuple[bytes | None, str]":
    """Extrai METADATA_BLOCK_PICTURE e TITLE= de OGG Vorbis/Opus."""
    import base64
    cover: bytes | None = None
    title: str = ""
    try:
        # Cover
        needle = b"METADATA_BLOCK_PICTURE="
        idx = data.upper().find(needle.upper())
        if idx != -1:
            start = idx + len(needle)
            end   = data.find(b"\x00", start)
            b64   = data[start: end if end != -1 else start + 4096]
            block = base64.b64decode(b64 + b"==")
            p = 4
            ml = struct.unpack(">I", block[p:p+4])[0]; p += 4 + ml
            dl = struct.unpack(">I", block[p:p+4])[0]; p += 4 + dl
            p += 16
            imglen = struct.unpack(">I", block[p:p+4])[0]; p += 4
            cover = block[p: p+imglen] or None

        # Title
        needle_t = b"TITLE="
        idx = data.upper().find(needle_t)
        if idx != -1:
            end = data.find(b"\x00", idx)
            raw = data[idx+6: end if end != -1 else idx + 256]
            title = raw.decode("utf-8", errors="ignore").strip()
    except (IndexError, TypeError, UnicodeError, ValueError):
        log_ignored_exception(__name__, "Could not parse OGG metadata")
    return cover, title


# ─────────────────────────────────────────────────────────────────────────────
# Threaded extractors — source I/O in workers, QPixmap creation in GUI thread
# ─────────────────────────────────────────────────────────────────────────────

class _ThreadedMediaInfoExtractor(QObject):
    info_ready = Signal(int, QPixmap, str)
    thumbnail_failed = Signal(int, object)

    _worker_ready = Signal(bytes, str)
    _worker_failed = Signal(object)

    def __init__(
        self,
        index: int,
        url: str,
        worker_pool: WorkerPool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._url = url
        self._worker_pool = worker_pool
        self._require_thumbnail = True
        self._require_title = True
        self._cancelled = CancellationFlag()
        self._worker: WorkerHandle | None = None
        cancelled = self._cancelled
        self.destroyed.connect(lambda *_: cancelled.set())
        self._worker_ready.connect(self._deliver_worker_result)
        self._worker_failed.connect(self._deliver_worker_failure)
        QTimer.singleShot(0, self._start)

    def set_request_intent(
        self,
        *,
        require_thumbnail: bool,
        require_title: bool,
        require_duration: bool,
    ) -> None:
        del require_duration
        self._require_thumbnail = require_thumbnail
        self._require_title = require_title

    def _start(self) -> None:
        if self._cancelled.is_set():
            return
        self._worker = self._worker_pool.submit(type(self).__name__, self._run)

    def cancel(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        self._cancelled.set()
        worker = self._worker
        if wait and worker is not None and not worker.is_current():
            worker.join(timeout=max(0.0, timeout))
        self.deleteLater()

    def _run(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            image_bytes, title = self._fetch_info()
        except Exception as exc:  # noqa: BLE001 - media metadata worker boundary
            log.debug("%s failed for %s: %s", type(self).__name__, self._url, exc)
            self._emit_worker_failure(_failure_from_exception(exc))
            return

        if self._cancelled.is_set():
            return
        try:
            self._worker_ready.emit(image_bytes or b"", title)
        except RuntimeError:
            return

    def _emit_worker_failure(self, failure: MediaInfoFailure) -> None:
        if self._cancelled.is_set():
            return
        try:
            self._worker_failed.emit(failure)
        except RuntimeError:
            return

    def _fetch_info(self) -> tuple[bytes | None, str]:
        raise NotImplementedError

    def _empty_result_is_authoritative(self) -> bool:
        return True

    def _deliver_worker_result(self, image_bytes: bytes, title: str) -> None:
        pixmap = QPixmap()
        if image_bytes:
            if not _embedded_image_is_complete(image_bytes):
                self.thumbnail_failed.emit(
                    self._index,
                    MediaInfoFailure(
                        MediaInfoFailureKind.FORMAT,
                        "The extracted embedded image is incomplete",
                        "incomplete-image",
                    ),
                )
                self.deleteLater()
                return
            pixmap.loadFromData(image_bytes)
            if pixmap.isNull():
                self.thumbnail_failed.emit(
                    self._index,
                    MediaInfoFailure(
                        MediaInfoFailureKind.FORMAT,
                        "The extracted embedded image could not be decoded",
                        "invalid-image",
                    ),
                )
                self.deleteLater()
                return
        if not pixmap.isNull() or title or self._empty_result_is_authoritative():
            self.info_ready.emit(self._index, pixmap, title)
        else:
            self.thumbnail_failed.emit(
                self._index,
                MediaInfoFailure(
                    MediaInfoFailureKind.FORMAT,
                    "The remote response did not contain a decodable image",
                    "invalid-image",
                ),
            )
        self.deleteLater()

    def _deliver_worker_failure(self, failure: MediaInfoFailure) -> None:
        self.thumbnail_failed.emit(self._index, failure)
        self.deleteLater()


class RemoteAudioInfoExtractor(_ThreadedMediaInfoExtractor):
    """
    Extrai cover art e título de áudio remoto sem baixar o arquivo completo.

    Faz uma requisição HTTP Range (bytes=0–524287, 512 KB máx) para obter
    apenas o header com metadados. Cover e título são lidos em memória pelos
    mesmos parsers usados para arquivos locais (ID3v2, MP4 atoms, FLAC, OGG).
    Nenhum byte além dos necessários é transferido.
    """

    _MAX_BYTES = 3_145_728  # 3 MB (3 * 1024 * 1024)
    _TIMEOUT_S = 10

    def __init__(
        self,
        index: int,
        url: str,
        worker_pool: WorkerPool,
        parent=None,
    ) -> None:
        self._ext = Path(url.split("?")[0]).suffix.lower()
        super().__init__(index, url, worker_pool, parent)

    def _fetch_info(self) -> tuple[bytes | None, str]:
        data = get_bytes(
            self._url,
            timeout=self._TIMEOUT_S,
            max_bytes=self._MAX_BYTES,
            headers={
                "Range": f"bytes=0-{self._MAX_BYTES - 1}",
                "User-Agent": "Mozilla/5.0",
            },
        )
        return _audio_info_from_bytes(data, self._ext)


class RemoteImageInfoExtractor(_ThreadedMediaInfoExtractor):
    """
    Downloads a remote image thumbnail with a bounded read.

    This is intentionally separate from the media cache: thumbnail files are
    small, derived UI assets and callers decide where/how to persist them.
    """

    _MAX_BYTES = 2 * 1024 * 1024
    _TIMEOUT_S = 12

    def _empty_result_is_authoritative(self) -> bool:
        return False

    def _fetch_info(self) -> tuple[bytes | None, str]:
        return get_bytes(
            self._url,
            timeout=self._TIMEOUT_S,
            max_bytes=self._MAX_BYTES,
            headers={
                "Range": f"bytes=0-{self._MAX_BYTES - 1}",
                "User-Agent": "Mozilla/5.0 (compatible; Solin/1.0)",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        ), ""


class LocalImageInfoExtractor(_ThreadedMediaInfoExtractor):
    """Read local/cloud-backed image bytes without blocking the Qt thread."""

    _MAX_BYTES = 64 * 1024 * 1024

    def _empty_result_is_authoritative(self) -> bool:
        return False

    def _fetch_info(self) -> tuple[bytes | None, str]:
        with open(self._url, "rb") as source:
            data = source.read(self._MAX_BYTES + 1)
        if len(data) > self._MAX_BYTES:
            raise ValueError("The local image exceeds the thumbnail decode limit")
        return data, ""


class LocalAudioInfoExtractor(_ThreadedMediaInfoExtractor):
    """Read local/cloud-backed audio metadata without blocking the Qt thread."""

    def _fetch_info(self) -> tuple[bytes | None, str]:
        return _read_audio_info_from_file(self._url)


# ─────────────────────────────────────────────────────────────────────────────
# MediaInfoExtractor — QMediaPlayer assíncrono (vídeo local/remoto, áudio local)
# ─────────────────────────────────────────────────────────────────────────────

class MediaInfoExtractor(QObject):
    """Thumbnail + title + duration extraction via the ffprobe/ffmpeg CLIs.

    libobs is the only media engine, so there is no ``QMediaPlayer`` to probe.
    Metadata is read out of band on a worker thread:

      1. ``ffprobe`` for the title tag, duration, and whether an embedded cover
         (attached picture / image stream) is present.
      2. embedded cover art via ``ffmpeg`` when present, else a decoded frame at
         ~5% of the duration.

    Signals (unchanged contract):
      info_ready(index, pixmap, title)  — extraction done (pixmap may be null)
      thumbnail_failed(index, failure)  — no decodable thumbnail for a video
      duration_ready(index, dur_ms)     — real duration (emitted before info_ready)

    The extraction is deferred to the next event-loop tick so a caller can set the
    request intent right after construction (as the existing call sites do).
    """

    info_ready       = Signal(int, QPixmap, str)   # (index, pixmap, title)
    thumbnail_failed = Signal(int, object)
    duration_ready   = Signal(int, int)            # (index, duration_ms)

    # Internal: worker-thread result hopped to the GUI thread (queued) so the
    # QPixmap is built there. Payload: (duration_ms, image_bytes|None, title).
    _result_ready = Signal(object)

    _THUMBNAIL_FRACTION = 0.05
    _MIN_THUMBNAIL_MS = 1000

    def __init__(
        self,
        index: int,
        url: str,
        media_type: str = "video",
        parent=None,
    ):
        super().__init__(parent)
        self._index          = index
        self._url            = url
        self._media_type     = media_type
        self._require_thumbnail = True
        self._require_title = True
        self._require_duration = False
        self._started = False
        self._cancelled = threading.Event()
        self.destroyed.connect(lambda *_a: self._cancelled.set())
        self._result_ready.connect(self._deliver)
        QTimer.singleShot(0, self._start)

    def set_require_duration(self, required: bool) -> None:
        self._require_duration = bool(required)

    def set_request_intent(
        self,
        *,
        require_thumbnail: bool,
        require_title: bool,
        require_duration: bool,
    ) -> None:
        self._require_thumbnail = require_thumbnail
        self._require_title = require_title
        self._require_duration = require_duration

    def cancel(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        del wait, timeout  # the worker is a daemon bounded by ffprobe/ffmpeg timeouts
        self._cancelled.set()
        self.deleteLater()

    # ── worker ────────────────────────────────────────────────────────────────

    def _start(self) -> None:
        if self._cancelled.is_set() or self._started:
            return
        self._started = True
        threading.Thread(
            target=self._run, name="solin-media-info", daemon=True,
        ).start()

    def _run(self) -> None:
        if self._cancelled.is_set():
            return
        duration_ms = 0
        title = ""
        image: bytes | None = None
        try:
            tags = ffprobe_metadata.probe_tags(self._url)
            if self._require_title:
                title = tags.title
            if self._require_duration:
                duration_ms = tags.duration_ms
            if self._require_thumbnail:
                if tags.cover_stream_index >= 0:
                    image = ffprobe_metadata.extract_cover(
                        self._url, tags.cover_stream_index,
                    )
                if not image:
                    at_ms = (
                        int(tags.duration_ms * self._THUMBNAIL_FRACTION)
                        if tags.duration_ms
                        else self._MIN_THUMBNAIL_MS
                    )
                    image = ffprobe_metadata.extract_thumbnail(self._url, at_ms)
        except Exception:  # noqa: BLE001 - metadata worker boundary (subprocess/IO)
            log.debug("media info extraction failed for %s", self._url, exc_info=True)
        if self._cancelled.is_set() or not shiboken6.isValid(self):
            return
        try:
            self._result_ready.emit((duration_ms, image, title))
        except RuntimeError:
            pass  # extractor deleted before the worker finished

    @Slot(object)
    def _deliver(self, payload) -> None:
        if self._cancelled.is_set():
            return
        duration_ms, image_bytes, title = payload
        if self._require_duration and duration_ms > 0:
            self.duration_ready.emit(self._index, duration_ms)
        if not self._require_thumbnail:
            self.info_ready.emit(self._index, QPixmap(), title)
            self.deleteLater()
            return
        pixmap = QPixmap()
        if image_bytes:
            pixmap.loadFromData(image_bytes)
        if not pixmap.isNull():
            self.info_ready.emit(self._index, pixmap, title)
        elif self._media_type == "audio":
            # The file opened but carries no embedded cover — a valid empty result.
            self.info_ready.emit(self._index, QPixmap(), title)
        else:
            self.thumbnail_failed.emit(
                self._index,
                MediaInfoFailure(
                    MediaInfoFailureKind.TRANSIENT,
                    "No decodable thumbnail could be extracted",
                    "thumbnail-unavailable",
                ),
            )
        self.deleteLater()


# ─────────────────────────────────────────────────────────────────────────────
# RemotePageMetaExtractor — og:image + og:title de qualquer URL web
# ─────────────────────────────────────────────────────────────────────────────

# Padrões para extrair og:title e og:image de HTML
_RE_OG_TITLE = re.compile(
    rb'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_RE_OG_TITLE_ALT = re.compile(
    rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    re.IGNORECASE,
)
_RE_OG_IMAGE = re.compile(
    rb'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_RE_OG_IMAGE_ALT = re.compile(
    rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)
_RE_TITLE_TAG = re.compile(rb'<title[^>]*>([^<]+)</title>', re.IGNORECASE)


def _extract_og_meta(data: bytes) -> "tuple[str, str]":
    """
    Extrai (thumb_url, title) de HTML via og:image / og:title / <title>.
    Retorna ('', '') se nada encontrado.
    """
    thumb_url = ""
    title = ""

    for pat in (_RE_OG_IMAGE, _RE_OG_IMAGE_ALT):
        m = pat.search(data)
        if m:
            thumb_url = m.group(1).decode("utf-8", errors="ignore").strip()
            break

    for pat in (_RE_OG_TITLE, _RE_OG_TITLE_ALT):
        m = pat.search(data)
        if m:
            title = m.group(1).decode("utf-8", errors="ignore").strip()
            break

    if not title:
        m = _RE_TITLE_TAG.search(data)
        if m:
            title = m.group(1).decode("utf-8", errors="ignore").strip()
            # Remove sufixos comuns de sites ("Título | Site" → "Título")
            for sep in (" | ", " - ", " – ", " — "):
                if sep in title:
                    title = title.split(sep)[0].strip()
                    break

    return thumb_url, title


class RemotePageMetaExtractor(_ThreadedMediaInfoExtractor):
    """
    Extrai thumbnail e título de qualquer URL remota (vídeo, página web)
    de forma independente, sem API externa e sem baixar a mídia completa.

    Estratégia:
      1. Faz requisição HTTP parcial (primeiros 96 KB) à URL.
      2. Se Content-Type for HTML → extrai og:image + og:title do HTML.
         Depois baixa a og:image (também parcial se necessário) como pixmap.
      3. Se Content-Type for mídia direta (video/audio) → extrai metadados
         de bytes (cover art ID3/MP4/FLAC) ou emite thumbnail_failed para que
         o MediaInfoExtractor (QMediaPlayer) seja usado.

    Completamente independente da API JW.org.
    """

    _MAX_HTML_BYTES  = 98_304   # 96 KB — suficiente para a maioria dos <head>
    _MAX_IMG_BYTES   = 524_288  # 512 KB para imagem og:image
    _TIMEOUT_S       = 12

    def _fetch_info(self) -> tuple[bytes | None, str]:
        response = http_get(
            self._url,
            timeout=self._TIMEOUT_S,
            max_bytes=self._MAX_HTML_BYTES,
            headers={
                "Range": f"bytes=0-{self._MAX_HTML_BYTES - 1}",
                "User-Agent": "Mozilla/5.0 (compatible; Solin/1.0)",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )
        content_type = response.headers.get("Content-Type", "").lower()
        data = response.content

        if "html" in content_type or data.lstrip()[:5].lower() in (b"<!doc", b"<html"):
            thumb_url, title = _extract_og_meta(data)
            image_bytes = (
                self._fetch_image_bytes(thumb_url)
                if self._require_thumbnail and thumb_url
                else None
            )
            return image_bytes, title

        if any(t in content_type for t in ("audio/", "mpeg")):
            ext = Path(self._url.split("?")[0]).suffix.lower()
            return _audio_info_from_bytes(data, ext)

        return None, ""

    def _fetch_image_bytes(self, img_url: str) -> bytes | None:
        try:
            return get_bytes(
                img_url,
                timeout=self._TIMEOUT_S,
                max_bytes=self._MAX_IMG_BYTES,
                headers={
                    "Range": f"bytes=0-{self._MAX_IMG_BYTES - 1}",
                    "User-Agent": "Mozilla/5.0 (compatible; Solin/1.0)",
                }
            )
        except (HttpError, OSError, ValueError) as exc:
            log.debug("Remote og:image fetch failed for %s: %s", img_url, exc)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# _RemoteVideoMetaThenStream — estratégia em dois estágios para vídeo remoto
# ─────────────────────────────────────────────────────────────────────────────

class _RemoteVideoMetaThenStream(QObject):
    """
    Para URLs remotas de vídeo/mídia: tenta primeiro extrair thumb+título via
    metadados HTML (og:image/og:title) sem tocar o player. Se falhar, cai para
    MediaInfoExtractor (QMediaPlayer streaming) que captura frame do vídeo.

    Isso resolve JW.org e qualquer outro site sem depender de API externa.
    A thumb vem do og:image da página (que normalmente é a capa/thumbnail do
    vídeo), e o título vem do og:title — ambos reais, não nomes de arquivo.
    """

    info_ready       = Signal(int, QPixmap, str)
    thumbnail_failed = Signal(int, object)
    duration_ready   = Signal(int, int)

    def __init__(
        self,
        index: int,
        url: str,
        media_type: str,
        worker_pool: WorkerPool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index      = index
        self._url        = url
        self._media_type = media_type
        self._done       = False
        self._require_thumbnail = True
        self._require_title = True
        self._require_duration = False
        self._page_title = ""
        self._page_pixmap = QPixmap()
        self._stream_ex: MediaInfoExtractor | None = None

        # Estágio 1: tenta og:image + og:title
        self._page_ex = RemotePageMetaExtractor(index, url, worker_pool, self)
        self._page_ex.info_ready.connect(self._on_page_ready)
        self._page_ex.thumbnail_failed.connect(self._on_page_failed)

    def set_request_intent(
        self,
        *,
        require_thumbnail: bool,
        require_title: bool,
        require_duration: bool,
    ) -> None:
        self._require_thumbnail = require_thumbnail
        self._require_title = require_title
        self._require_duration = require_duration
        self._page_ex.set_request_intent(
            require_thumbnail=require_thumbnail,
            require_title=require_title,
            require_duration=False,
        )

    def _on_page_ready(self, index: int, pixmap: QPixmap, title: str):
        if self._done:
            return
        self._page_title = title
        if pixmap is not None and not pixmap.isNull():
            self._page_pixmap = pixmap
        if not self._require_duration and (
            not self._require_thumbnail or not self._page_pixmap.isNull()
        ):
            self._done = True
            self.info_ready.emit(index, self._page_pixmap, title)
            QTimer.singleShot(0, self.deleteLater)
            return
        self._start_stream(index)

    def _on_page_failed(self, index: int, failure: MediaInfoFailure):
        if self._done:
            return
        if (
            failure.kind is MediaInfoFailureKind.PERMANENT
            or (not self._require_thumbnail and not self._require_duration)
        ):
            self._done = True
            self.thumbnail_failed.emit(index, failure)
            QTimer.singleShot(0, self.deleteLater)
            return
        self._start_stream(index)

    def _start_stream(self, index: int) -> None:
        if self._stream_ex is not None:
            return
        # Estágio 2: fallback para QMediaPlayer (captura frame do stream)
        self._stream_ex = MediaInfoExtractor(
            index,
            self._url,
            self._media_type,
            self,
        )
        self._stream_ex.set_request_intent(
            require_thumbnail=(
                self._require_thumbnail and self._page_pixmap.isNull()
            ),
            require_title=(self._require_title and not self._page_title),
            require_duration=self._require_duration,
        )
        self._stream_ex.info_ready.connect(self._on_stream_ready)
        self._stream_ex.thumbnail_failed.connect(self._on_stream_failed)
        self._stream_ex.duration_ready.connect(self.duration_ready)

    def _on_stream_ready(self, index: int, pixmap: QPixmap, title: str):
        if self._done:
            return
        result_pixmap = (
            self._page_pixmap
            if not self._page_pixmap.isNull()
            else pixmap
        )
        if (
            self._require_thumbnail
            and (result_pixmap is None or result_pixmap.isNull())
        ):
            self._on_stream_failed(
                index,
                MediaInfoFailure(
                    MediaInfoFailureKind.FORMAT,
                    "The remote video did not yield a thumbnail",
                    "no-video-frame",
                ),
            )
            return
        self._done = True
        self.info_ready.emit(index, result_pixmap, title or self._page_title)
        QTimer.singleShot(0, self.deleteLater)

    def _on_stream_failed(self, index: int, failure: MediaInfoFailure):
        if self._done:
            return
        self._done = True
        self.thumbnail_failed.emit(index, failure)
        QTimer.singleShot(0, self.deleteLater)

    def cancel(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        if self._done:
            return
        self._done = True
        deadline = time.monotonic() + max(0.0, timeout)
        for extractor in (self._page_ex, self._stream_ex):
            if extractor is not None:
                cancel = getattr(extractor, "cancel", None)
                if callable(cancel):
                    try:
                        remaining = max(0.0, deadline - time.monotonic())
                        cancel(wait=wait, timeout=remaining)
                    except TypeError:
                        cancel()
                    except RuntimeError:
                        pass
        self.deleteLater()


class _LocalAudioMetaThenPlayer(QObject):
    """Read audio metadata in a worker, then use Qt only for missing fields."""

    info_ready = Signal(int, QPixmap, str)
    thumbnail_failed = Signal(int, object)
    duration_ready = Signal(int, int)

    def __init__(
        self,
        index: int,
        url: str,
        media_type: str,
        worker_pool: WorkerPool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._url = url
        self._media_type = media_type
        self._done = False
        self._require_thumbnail = True
        self._require_title = True
        self._require_duration = False
        self._metadata_pixmap = QPixmap()
        self._metadata_title = ""
        self._metadata_image_failed = False
        self._player_ex: MediaInfoExtractor | None = None

        self._metadata_ex = LocalAudioInfoExtractor(
            index,
            url,
            worker_pool,
            self,
        )
        self._metadata_ex.info_ready.connect(self._on_metadata_ready)
        self._metadata_ex.thumbnail_failed.connect(self._on_metadata_failed)

    def set_request_intent(
        self,
        *,
        require_thumbnail: bool,
        require_title: bool,
        require_duration: bool,
    ) -> None:
        self._require_thumbnail = require_thumbnail
        self._require_title = require_title
        self._require_duration = require_duration
        self._metadata_ex.set_request_intent(
            require_thumbnail=require_thumbnail,
            require_title=require_title,
            require_duration=False,
        )

    def _on_metadata_ready(self, index: int, pixmap: QPixmap, title: str) -> None:
        if self._done:
            return
        if pixmap is not None and not pixmap.isNull():
            self._metadata_pixmap = pixmap
        self._metadata_title = title
        metadata_satisfies_request = (
            not self._require_duration
            and (not self._require_thumbnail or not self._metadata_pixmap.isNull())
            and (not self._require_title or bool(self._metadata_title))
        )
        if metadata_satisfies_request:
            self._finish(index, self._metadata_pixmap, self._metadata_title)
            return
        self._start_player(index)

    def _on_metadata_failed(
        self,
        index: int,
        failure: MediaInfoFailure,
    ) -> None:
        if self._done:
            return
        if failure.kind is MediaInfoFailureKind.FORMAT:
            self._metadata_image_failed = True
            self._start_player(index)
            return
        self._done = True
        self.thumbnail_failed.emit(index, failure)
        QTimer.singleShot(0, self.deleteLater)

    def _start_player(self, index: int) -> None:
        if self._player_ex is not None:
            return
        self._player_ex = MediaInfoExtractor(
            index,
            self._url,
            self._media_type,
            self,
        )
        self._player_ex.set_request_intent(
            require_thumbnail=(
                self._require_thumbnail and self._metadata_pixmap.isNull()
            ),
            require_title=(self._require_title and not self._metadata_title),
            require_duration=self._require_duration,
        )
        self._player_ex.info_ready.connect(self._on_player_ready)
        self._player_ex.thumbnail_failed.connect(self._on_player_failed)
        self._player_ex.duration_ready.connect(self.duration_ready)

    def _on_player_ready(self, index: int, pixmap: QPixmap, title: str) -> None:
        if self._done:
            return
        result_pixmap = (
            self._metadata_pixmap
            if not self._metadata_pixmap.isNull()
            else pixmap
        )
        if (
            self._require_thumbnail
            and (result_pixmap is None or result_pixmap.isNull())
            and self._metadata_image_failed
        ):
            self._on_player_failed(
                index,
                MediaInfoFailure(
                    MediaInfoFailureKind.FORMAT,
                    "The local audio did not yield a thumbnail",
                    "no-audio-cover",
                ),
            )
            return
        self._finish(index, result_pixmap, title or self._metadata_title)

    def _on_player_failed(self, index: int, failure: MediaInfoFailure) -> None:
        if self._done:
            return
        self._done = True
        self.thumbnail_failed.emit(index, failure)
        QTimer.singleShot(0, self.deleteLater)

    def _finish(self, index: int, pixmap: QPixmap, title: str) -> None:
        self._done = True
        self.info_ready.emit(index, pixmap, title)
        QTimer.singleShot(0, self.deleteLater)

    def cancel(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        if self._done:
            return
        self._done = True
        deadline = time.monotonic() + max(0.0, timeout)
        for extractor in (self._metadata_ex, self._player_ex):
            if extractor is None:
                continue
            cancel = getattr(extractor, "cancel", None)
            if not callable(cancel):
                continue
            try:
                remaining = max(0.0, deadline - time.monotonic())
                cancel(wait=wait, timeout=remaining)
            except TypeError:
                cancel()
            except RuntimeError:
                pass
        self.deleteLater()


ExtractorFactory = Callable[[int, str, str, WorkerPool, QObject], QObject]


def _remote_image_factory(
    index: int,
    url: str,
    _media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return RemoteImageInfoExtractor(index, url, worker_pool, parent)


def _local_image_factory(
    index: int,
    url: str,
    _media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return LocalImageInfoExtractor(index, url, worker_pool, parent)


def _remote_audio_factory(
    index: int,
    url: str,
    _media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return RemoteAudioInfoExtractor(index, url, worker_pool, parent)


def _local_audio_factory(
    index: int,
    url: str,
    media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return _LocalAudioMetaThenPlayer(
        index,
        url,
        media_type,
        worker_pool,
        parent,
    )


def _remote_video_factory(
    index: int,
    url: str,
    media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return _RemoteVideoMetaThenStream(index, url, media_type, worker_pool, parent)


def _local_media_factory(
    index: int,
    url: str,
    media_type: str,
    _worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    return MediaInfoExtractor(index, url, media_type, parent)


_EXTRACTOR_FACTORIES: dict[tuple[bool, str | None], ExtractorFactory] = {
    (True, "image"): _remote_image_factory,
    (True, "audio"): _remote_audio_factory,
    (True, None): _remote_video_factory,
    (False, "image"): _local_image_factory,
    (False, "audio"): _local_audio_factory,
    (False, None): _local_media_factory,
}


def _create_extractor(
    index: int,
    url: str,
    media_type: str,
    worker_pool: WorkerPool,
    parent: QObject,
) -> QObject:
    is_remote = url.startswith(("http://", "https://"))
    factory = _EXTRACTOR_FACTORIES.get((is_remote, media_type))
    if factory is None:
        factory = _EXTRACTOR_FACTORIES[(is_remote, None)]
    return factory(index, url, media_type, worker_pool, parent)


class MediaInfoQueue(QObject):
    """
    Gerencia a extração de thumbnail + título em fila, com no máximo
    _MAX_CONCURRENT extratores simultâneos.

    ``info_ready`` encerra a requisição. Quando houver duração disponível,
    ``duration_ready`` é sempre emitido antes desse sinal terminal.
    Falhas de processamento não são confundidas com ausência de imagem:
    ``request_failed`` só é emitido quando a política de retry se encerra.

    Sinal principal:
      info_ready(index, pixmap, title)
        - pixmap válido = thumbnail encontrada
        - pixmap nulo   = nenhuma imagem, mas pode haver título
        - title         = título dos metadados, ou "" se não encontrado

    Para alimentação ao vivo (frame/cover capturado do player em reprodução):
      feed_live_frame(index, pixmap)  — apenas thumbnail, sem título
      feed_live_cover(index, pixmap)  — cover art, prioridade máxima
    Ambos emitem info_ready com title="".
    """

    info_ready     = Signal(int, QPixmap, str)   # (index, pixmap, title)
    duration_ready = Signal(int, int)            # (index, duration_ms)
    request_failed = Signal(int, object)          # terminal processing failure
    _diskCacheLoaded = Signal(object, object)
    _sourceIdentityChecked = Signal(object, str)

    _MAX_CONCURRENT = 2

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        thumb_cache_dir: str | os.PathLike[str],
        worker_pool: WorkerPool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._thumb_cache_dir = os.fspath(thumb_cache_dir)
        self._worker_pool = worker_pool
        self._scheduler = MediaInfoScheduler(
            max_concurrent=self._MAX_CONCURRENT,
        )
        self._cache: dict[int, tuple[QPixmap, str]] = {}
        self._duration_cache: dict[int, int] = {}
        self._extractors: dict[int, QObject] = {}
        self._request_states: dict[int, _MediaInfoRequestState] = {}
        self._retry_timers: dict[int, QTimer] = {}
        self._terminal_source_failures: dict[str, MediaInfoFailure] = {}
        self._disk_cache_pending: list[_DiskCacheLookup] = []
        self._disk_cache_active: dict[int, _DiskCacheLookup] = {}
        self._pending_results: dict[int, _PendingMediaInfoResult] = {}
        self._diskCacheLoaded.connect(self._on_disk_cache_loaded)
        self._sourceIdentityChecked.connect(self._on_source_identity_checked)

    # ── Disk Cache ────────────────────────────────────────────────────────────

    @staticmethod
    def _failure_identity(url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return os.path.normcase(os.path.abspath(url))

    def _disk_cache_is_scheduled(self, index: int) -> bool:
        return index in self._disk_cache_active or any(
            lookup.index == index for lookup in self._disk_cache_pending
        )

    def _schedule_disk_cache_lookup(self, lookup: _DiskCacheLookup) -> None:
        self._disk_cache_pending.append(lookup)
        self._pump_disk_cache()

    def _pump_disk_cache(self) -> None:
        while (
            self._disk_cache_pending
            and len(self._disk_cache_active) < self._MAX_CONCURRENT
        ):
            lookup = self._disk_cache_pending.pop(0)
            if not self._scheduler.is_current(lookup.version):
                continue
            self._disk_cache_active[lookup.index] = lookup

            def load(current: _DiskCacheLookup = lookup) -> None:
                result = _load_media_info_disk_cache(
                    self._thumb_cache_dir,
                    current.url,
                    load_thumbnail=current.require_thumbnail,
                )
                try:
                    self._diskCacheLoaded.emit(current, result)
                except RuntimeError:
                    pass

            try:
                worker = self._worker_pool.submit("media-info-cache-read", load)
            except RuntimeError:
                worker = None
            if worker is not None:
                continue
            self._disk_cache_active.pop(lookup.index, None)
            self._begin_extraction_after_cache(lookup)

    @Slot(object, object)
    def _on_disk_cache_loaded(self, lookup: object, result: object) -> None:
        if not isinstance(lookup, _DiskCacheLookup) or not isinstance(
            result,
            _DiskCacheResult,
        ):
            return
        if self._disk_cache_active.get(lookup.index) != lookup:
            return
        self._disk_cache_active.pop(lookup.index, None)
        if not self._scheduler.is_current(lookup.version):
            self._pump_disk_cache()
            return

        cache_fulfils_request = (
            result.hit
            and (not lookup.require_title or result.title_resolved)
            and (not lookup.require_duration or result.duration_ms > 0)
        )
        if cache_fulfils_request:
            pixmap = (
                QPixmap.fromImage(result.image)
                if result.image is not None
                else QPixmap()
            )
            result_title = result.title if lookup.require_title else ""
            self._cache[lookup.index] = (pixmap, result_title)
            if lookup.require_duration and result.duration_ms > 0:
                self._duration_cache[lookup.index] = result.duration_ms
                QTimer.singleShot(
                    0,
                    lambda v=lookup.version, i=lookup.index, duration=result.duration_ms:
                        self._emit_duration_if_current(v, i, duration),
                )
            self._emit_info_later(
                lookup.version,
                lookup.index,
                pixmap,
                result_title,
            )
        else:
            self._begin_extraction_after_cache(
                lookup,
                source_identity=result.source_identity,
            )
        self._pump_disk_cache()

    def _schedule_disk_cache_save(
        self,
        url: str,
        image: QImage,
        title: str,
        *,
        title_resolved: bool,
        duration_ms: int,
        source_identity: str,
    ) -> None:
        cached_image = QImage(image)

        def save() -> None:
            _save_media_info_disk_cache(
                self._thumb_cache_dir,
                url,
                cached_image,
                title,
                title_resolved=title_resolved,
                duration_ms=duration_ms,
                expected_source_identity=source_identity,
            )

        try:
            self._worker_pool.submit("media-info-cache-write", save)
        except RuntimeError:
            pass

    # ── API pública ───────────────────────────────────────────────────────────

    def request(
        self,
        index: int,
        url: str,
        media_type: str = "video",
        *,
        require_thumbnail: bool = True,
        require_title: bool = True,
        require_duration: bool = False,
        restart_on_source_change: bool = True,
    ) -> None:
        """Solicita extração de info para o item. Idempotente."""
        if (
            self._scheduler.is_scheduled(index)
            or index in self._request_states
            or index in self._retry_timers
            or self._disk_cache_is_scheduled(index)
        ):
            return
        if index in self._cache:
            if not require_duration or self._duration_cache.get(index, 0) > 0:
                return
            self._cache.pop(index, None)

        self._schedule_disk_cache_lookup(
            _DiskCacheLookup(
                version=self._scheduler.version_for(index),
                index=index,
                url=url,
                media_type=media_type,
                require_thumbnail=require_thumbnail,
                require_title=require_title,
                require_duration=require_duration,
                restart_on_source_change=restart_on_source_change,
            )
        )

    def _begin_extraction_after_cache(
        self,
        lookup: _DiskCacheLookup,
        *,
        source_identity: str = "",
    ) -> None:
        if not self._scheduler.is_current(lookup.version):
            return

        index = lookup.index
        url = lookup.url
        media_type = lookup.media_type
        require_thumbnail = lookup.require_thumbnail
        require_title = lookup.require_title
        require_duration = lookup.require_duration
        is_remote = url.startswith(("http://", "https://"))
        terminal_failure = self._terminal_source_failures.get(
            self._failure_identity(url)
        )
        if terminal_failure is not None:
            fallback = None
            if is_remote:
                try:
                    fallback = completed_cached_path(url, self._media_cache_dir)
                except (OSError, UnicodeError, ValueError):
                    pass
            if fallback:
                self._request_states[index] = _MediaInfoRequestState(
                    source_url=url,
                    source_identity=source_identity,
                    media_type=media_type,
                    require_thumbnail=require_thumbnail,
                    require_title=require_title,
                    require_duration=require_duration,
                    restart_on_source_change=lookup.restart_on_source_change,
                    fallback_url=fallback,
                    origin_failure=terminal_failure,
                )
                self._enqueue(
                    index,
                    fallback,
                    media_type,
                    require_thumbnail=require_thumbnail,
                    require_title=require_title,
                    require_duration=require_duration,
                )
                self._pump()
            else:
                self._emit_terminal_failure_later(index, terminal_failure)
            return

        self._request_states[index] = _MediaInfoRequestState(
            source_url=url,
            source_identity=source_identity,
            media_type=media_type,
            require_thumbnail=require_thumbnail,
            require_title=require_title,
            require_duration=require_duration,
            restart_on_source_change=lookup.restart_on_source_change,
        )

        # A thumbnail derivada já foi tentada acima. Para uma origem remota,
        # consulte o servidor primeiro; o arquivo completo local só é usado
        # como fallback depois de uma falha real da origem.
        self._enqueue(
            index,
            url,
            media_type,
            require_thumbnail=require_thumbnail,
            require_title=require_title,
            require_duration=require_duration,
        )
        self._pump()

    def feed_live_frame(self, index: int, pixmap: QPixmap) -> bool:
        """
        Alimenta com frame capturado ao vivo.
        Só aceita se ainda não há thumbnail válido.
        """
        if not pixmap or pixmap.isNull():
            return False
        existing_px, existing_title = self._cache.get(index, (None, ""))
        if existing_px is not None and not existing_px.isNull():
            return False
        self.invalidate(index)
        self._cache[index] = (pixmap, existing_title or "")
        self.info_ready.emit(index, pixmap, "")
        return True

    def feed_live_cover(self, index: int, pixmap: QPixmap) -> bool:
        """
        Alimenta com cover art ao vivo. Prioridade máxima — sempre sobrescreve.
        """
        if not pixmap or pixmap.isNull():
            return False
        _, existing_title = self._cache.get(index, (None, ""))
        self.invalidate(index)
        self._cache[index] = (pixmap, existing_title or "")
        self.info_ready.emit(index, pixmap, "")
        return True

    def get_cached(self, index: int) -> "tuple[QPixmap | None, str]":
        """Retorna (pixmap, title) do cache, ou (None, '') se ausente."""
        return self._cache.get(index, (None, ""))

    def get_cached_pixmap(self, index: int) -> "QPixmap | None":
        px, _ = self._cache.get(index, (None, ""))
        return px

    def is_cached(self, index: int) -> bool:
        return index in self._cache

    def has_real_thumb(self, index: int) -> bool:
        px, _ = self._cache.get(index, (None, ""))
        return px is not None and not px.isNull()

    def invalidate(self, index: int):
        self._cancel_retry(index)
        self._request_states.pop(index, None)
        self._disk_cache_pending = [
            lookup for lookup in self._disk_cache_pending if lookup.index != index
        ]
        self._disk_cache_active.pop(index, None)
        self._pending_results.pop(index, None)
        for job in self._scheduler.invalidate(index):
            self._cancel_job(job)
        self._cache.pop(index, None)
        self._duration_cache.pop(index, None)

    def clear(self):
        for index in tuple(self._retry_timers):
            self._cancel_retry(index)
        active_jobs = self._scheduler.clear()
        self._disk_cache_pending.clear()
        self._disk_cache_active.clear()
        self._pending_results.clear()
        self._request_states.clear()
        self._cache.clear()
        self._duration_cache.clear()
        for job in active_jobs:
            self._cancel_job(job)

    def shutdown(self, timeout: float = 2.0) -> None:
        for index in tuple(self._retry_timers):
            self._cancel_retry(index)
        active_jobs = self._scheduler.shutdown()
        self._disk_cache_pending.clear()
        self._disk_cache_active.clear()
        self._pending_results.clear()
        self._request_states.clear()
        self._terminal_source_failures.clear()
        self._cache.clear()
        deadline = time.monotonic() + max(0.0, timeout)
        for job in active_jobs:
            remaining = max(0.0, deadline - time.monotonic())
            self._cancel_job(job, wait=True, timeout=remaining)

    # ── Interno ───────────────────────────────────────────────────────────────

    def _cancel_retry(self, index: int) -> None:
        timer = self._retry_timers.pop(index, None)
        if timer is None:
            return
        timer.stop()
        timer.deleteLater()

    def _enqueue(
        self,
        index: int,
        url: str,
        media_type: str,
        *,
        require_thumbnail: bool = True,
        require_title: bool = True,
        require_duration: bool = False,
    ) -> None:
        self._scheduler.enqueue(
            index,
            url,
            media_type,
            require_thumbnail=require_thumbnail,
            require_title=require_title,
            require_duration=require_duration,
        )

    def _emit_info_later(
        self,
        version: MediaInfoVersion,
        index: int,
        pixmap: QPixmap,
        title: str,
    ) -> None:
        """Emit fast-path results asynchronously, matching queued extractors."""
        queued_pixmap = QPixmap(pixmap)
        QTimer.singleShot(
            0,
            lambda v=version, i=index, px=queued_pixmap, t=title:
                self._emit_info_if_current(v, i, px, t)
        )

    def _emit_terminal_failure_later(
        self,
        index: int,
        failure: MediaInfoFailure,
    ) -> None:
        version = self._scheduler.version_for(index)
        QTimer.singleShot(
            0,
            lambda v=version, i=index, current=failure:
                self._emit_terminal_failure_if_current(v, i, current),
        )

    def _emit_terminal_failure_if_current(
        self,
        version: MediaInfoVersion,
        index: int,
        failure: MediaInfoFailure,
    ) -> bool:
        if (
            version.index != index
            or not self._scheduler.is_current(version)
            or self._scheduler.is_scheduled(index)
            or index in self._request_states
            or index in self._retry_timers
        ):
            return False
        self.request_failed.emit(index, failure)
        return True

    def _emit_info_if_current(
        self,
        version: MediaInfoVersion,
        index: int,
        pixmap: QPixmap,
        title: str,
    ) -> bool:
        if (
            version.index != index
            or not self._scheduler.is_current(version)
            or index not in self._cache
        ):
            return False
        self.info_ready.emit(index, pixmap, title)
        return True

    def _emit_duration_if_current(
        self,
        version: MediaInfoVersion,
        index: int,
        duration_ms: int,
    ) -> bool:
        if (
            version.index != index
            or not self._scheduler.is_current(version)
            or self._duration_cache.get(index) != duration_ms
        ):
            return False
        self.duration_ready.emit(index, duration_ms)
        return True

    def _pump(self):
        while jobs := self._scheduler.pump(blocked_indices=self._cache):
            for job in jobs:
                self._start_job(job)

    def _start_job(self, job: MediaInfoJob) -> None:
        try:
            ex = _create_extractor(
                job.index,
                job.url,
                job.media_type,
                self._worker_pool,
                self,
            )
        except Exception as exc:  # noqa: BLE001 - Qt factory boundary isolates one job
            if not self._scheduler.fail(job, job.index):
                return
            log.exception(
                "Could not create media info extractor for %s",
                job.url,
            )
            self._handle_failure(
                job,
                job.index,
                _failure_from_exception(exc),
                pump=False,
            )
            return
        self._attach_extractor(job, ex)

    def _attach_extractor(
        self,
        job: MediaInfoJob,
        ex: QObject,
        *,
        require_thumbnail: bool | None = None,
        require_title: bool | None = None,
        require_duration: bool | None = None,
    ) -> None:
        thumbnail_required = (
            job.require_thumbnail
            if require_thumbnail is None
            else require_thumbnail
        )
        title_required = job.require_title if require_title is None else require_title
        duration_required = (
            job.require_duration
            if require_duration is None
            else require_duration
        )
        configure_intent = getattr(ex, "set_request_intent", None)
        if callable(configure_intent):
            configure_intent(
                require_thumbnail=thumbnail_required,
                require_title=title_required,
                require_duration=duration_required,
            )
        else:
            configure_duration = getattr(ex, "set_require_duration", None)
            if callable(configure_duration):
                configure_duration(duration_required)
        self._extractors[job.index] = ex
        extractor_signals = cast(Any, ex)
        extractor_signals.info_ready.connect(
            lambda index, pixmap, title, current=job:
                self._on_ready(current, index, pixmap, title)
        )
        extractor_signals.thumbnail_failed.connect(
            lambda index, failure=None, current=job:
                self._on_failed(current, index, failure)
        )
        duration_ready = getattr(ex, "duration_ready", None)
        if duration_ready is not None:
            duration_ready.connect(
                lambda index, dur_ms, current=job:
                    self._on_duration_ext(current, index, dur_ms)
            )

    def _start_remote_audio_duration_fallback(
        self,
        job: MediaInfoJob,
    ) -> None:
        try:
            extractor = MediaInfoExtractor(
                job.index,
                job.url,
                job.media_type,
                self,
            )
        except Exception as exc:  # noqa: BLE001 - Qt factory boundary
            self._on_failed(job, job.index, _failure_from_exception(exc))
            return
        self._attach_extractor(
            job,
            extractor,
            require_thumbnail=False,
            require_title=False,
            require_duration=True,
        )

    def _cancel_job(
        self,
        job: MediaInfoJob,
        *,
        wait: bool = False,
        timeout: float = 0.0,
    ) -> None:
        extractor = self._extractors.pop(job.index, None)
        if extractor is None:
            return
        cancel = getattr(extractor, "cancel", None)
        if callable(cancel):
            try:
                cancel(wait=wait, timeout=timeout)
            except TypeError:
                cancel()
            except RuntimeError:
                pass
        else:
            try:
                extractor.deleteLater()
            except RuntimeError:
                pass

    def _on_duration_ext(
        self,
        job: MediaInfoJob,
        index: int,
        dur_ms: int,
    ):
        if not (
            job.require_duration
            and self._scheduler.accepts_result(job, index)
            and dur_ms > 0
        ):
            return
        state = self._request_states.get(index)
        if state is None:
            return
        state.partial_duration_ms = dur_ms
        if (
            state.source_url.startswith(("http://", "https://"))
            or not state.source_identity
        ):
            self._publish_duration(index, dur_ms)

    def _on_ready(
        self,
        job: MediaInfoJob,
        index: int,
        pixmap: QPixmap,
        title: str,
    ):
        if not self._scheduler.accepts_result(job, index):
            return
        state = self._request_states.get(index)
        if state is not None and state.partial_pixmap is not None:
            if pixmap is None or pixmap.isNull():
                pixmap = state.partial_pixmap
            title = title or state.partial_title
        if (
            state is not None
            and state.require_duration
            and state.partial_duration_ms <= 0
            and state.media_type == "audio"
            and state.source_url.startswith(("http://", "https://"))
        ):
            if state.duration_fallback_started:
                self._on_failed(
                    job,
                    index,
                    MediaInfoFailure(
                        MediaInfoFailureKind.TRANSIENT,
                        "The remote audio did not yield its duration",
                        "duration-unavailable",
                    ),
                )
                return
            state.partial_pixmap = pixmap if state.require_thumbnail else None
            state.partial_title = title if state.require_title else ""
            state.duration_fallback_started = True
            self._extractors.pop(index, None)
            self._start_remote_audio_duration_fallback(job)
            return
        if (
            state is not None
            and state.source_identity
            and not state.source_url.startswith(("http://", "https://"))
        ):
            self._extractors.pop(index, None)
            self._schedule_source_identity_check(job, pixmap, title)
            return
        self._finalize_ready(job, index, pixmap, title)

    def _schedule_source_identity_check(
        self,
        job: MediaInfoJob,
        pixmap: QPixmap,
        title: str,
    ) -> None:
        pending = _PendingMediaInfoResult(job, QPixmap(pixmap), title)
        self._pending_results[job.index] = pending
        state = self._request_states.get(job.index)
        source_url = state.source_url if state is not None else ""

        def validate() -> None:
            current_identity = _media_info_cache_source_identity(source_url)
            try:
                self._sourceIdentityChecked.emit(job, current_identity)
            except RuntimeError:
                pass

        try:
            worker = self._worker_pool.submit("media-info-cache-validate", validate)
        except RuntimeError:
            worker = None
        if worker is not None:
            return
        self._pending_results.pop(job.index, None)
        if self._scheduler.fail(job, job.index):
            self._request_states.pop(job.index, None)
            self._pump()

    @Slot(object, str)
    def _on_source_identity_checked(
        self,
        job: object,
        current_identity: str,
    ) -> None:
        if not isinstance(job, MediaInfoJob):
            return
        pending = self._pending_results.get(job.index)
        if pending is None or pending.job is not job:
            return
        self._pending_results.pop(job.index, None)
        if not self._scheduler.accepts_result(job, job.index):
            return
        state = self._request_states.get(job.index)
        if state is None:
            return
        if current_identity != state.source_identity:
            if not self._scheduler.complete(job, job.index):
                return
            self._duration_cache.pop(job.index, None)
            if state.restart_on_source_change:
                state.source_change_attempts += 1
                if state.source_change_attempts <= len(
                    _SOURCE_CHANGE_RETRY_DELAYS_MS
                ):
                    state.source_identity = current_identity
                    state.partial_pixmap = None
                    state.partial_title = ""
                    state.partial_duration_ms = 0
                    state.duration_fallback_started = False
                    version = self._scheduler.version_for(job.index)
                    timer = QTimer(self)
                    timer.setSingleShot(True)
                    timer.timeout.connect(
                        lambda i=job.index, v=version:
                            self._retry_changed_source(i, v)
                    )
                    self._retry_timers[job.index] = timer
                    timer.start(
                        _SOURCE_CHANGE_RETRY_DELAYS_MS[
                            state.source_change_attempts - 1
                        ]
                    )
                    self._pump()
                    return
            self._request_states.pop(job.index, None)
            self.request_failed.emit(
                job.index,
                MediaInfoFailure(
                    MediaInfoFailureKind.TRANSIENT,
                    "The media source changed during extraction",
                    "source-changed",
                ),
            )
            self._pump()
            return
        self._finalize_ready(
            job,
            job.index,
            pending.pixmap,
            pending.title,
        )

    def _retry_changed_source(
        self,
        index: int,
        version: MediaInfoVersion,
    ) -> None:
        timer = self._retry_timers.pop(index, None)
        if timer is not None:
            timer.deleteLater()
        state = self._request_states.get(index)
        if state is None or not self._scheduler.is_current(version):
            return
        self._enqueue(
            index,
            state.source_url,
            state.media_type,
            require_thumbnail=state.require_thumbnail,
            require_title=state.require_title,
            require_duration=state.require_duration,
        )
        self._pump()

    def _finalize_ready(
        self,
        job: MediaInfoJob,
        index: int,
        pixmap: QPixmap,
        title: str,
    ) -> None:
        state = self._request_states.get(index)
        if (
            state is not None
            and state.require_duration
            and state.partial_duration_ms > 0
        ):
            self._publish_duration(index, state.partial_duration_ms)
        if (
            state is not None
            and state.require_thumbnail
            and (pixmap is None or pixmap.isNull())
            and (
                state.media_type != "audio"
                or state.embedded_image_failed
            )
        ):
            if not self._scheduler.complete(job, index):
                return
            self._extractors.pop(index, None)
            self._handle_failure(
                job,
                index,
                MediaInfoFailure(
                    MediaInfoFailureKind.FORMAT,
                    (
                        "The embedded image could not be decoded"
                        if state.embedded_image_failed
                        else "Extraction completed without a usable thumbnail"
                    ),
                    (
                        "invalid-embedded-image"
                        if state.embedded_image_failed
                        else "empty-thumbnail"
                    ),
                ),
            )
            return
        if not self._scheduler.complete(job, index):
            return
        self._extractors.pop(index, None)
        state = self._request_states.pop(index, None)
        if state is None:
            self._pump()
            return
        self._terminal_source_failures.pop(
            self._failure_identity(state.source_url),
            None,
        )
        result_pixmap = pixmap if state.require_thumbnail else QPixmap()
        result_title = title if state.require_title else ""
        if state.require_thumbnail:
            self._schedule_disk_cache_save(
                state.source_url,
                result_pixmap.toImage(),
                result_title,
                title_resolved=state.require_title,
                duration_ms=self._duration_cache.get(index, 0),
                source_identity=state.source_identity,
            )
        self._cache[index] = (result_pixmap, result_title)
        self.info_ready.emit(index, result_pixmap, result_title)
        self._pump()

    def _publish_duration(self, index: int, duration_ms: int) -> None:
        if self._duration_cache.get(index) == duration_ms:
            return
        self._duration_cache[index] = duration_ms
        self.duration_ready.emit(index, duration_ms)

    def _on_failed(
        self,
        job: MediaInfoJob,
        index: int,
        failure: object = None,
    ) -> None:
        self._pending_results.pop(index, None)
        if not self._scheduler.fail(job, index):
            return
        self._extractors.pop(index, None)
        classified = (
            failure
            if isinstance(failure, MediaInfoFailure)
            else MediaInfoFailure(
                MediaInfoFailureKind.TRANSIENT,
                str(failure or "Thumbnail extraction failed"),
                "unknown",
            )
        )
        self._handle_failure(job, index, classified)

    def _handle_failure(
        self,
        job: MediaInfoJob,
        index: int,
        failure: MediaInfoFailure,
        *,
        pump: bool = True,
    ) -> None:
        state = self._request_states.get(index)
        if state is None:
            return

        source_is_remote = state.source_url.startswith(("http://", "https://"))
        is_origin_attempt = job.url == state.source_url
        if source_is_remote and is_origin_attempt:
            try:
                fallback = completed_cached_path(
                    state.source_url,
                    self._media_cache_dir,
                )
            except (OSError, UnicodeError, ValueError):
                fallback = None
            if fallback:
                state.fallback_url = fallback
                state.origin_failure = failure
                self._enqueue(
                    index,
                    fallback,
                    state.media_type,
                    require_thumbnail=state.require_thumbnail,
                    require_title=state.require_title,
                    require_duration=state.require_duration,
                )
                if pump:
                    self._pump()
                return

        retry_target = state.source_url
        effective_failure = failure
        if state.fallback_url and job.url == state.fallback_url:
            origin_failure = state.origin_failure
            if (
                origin_failure is not None
                and origin_failure.kind is MediaInfoFailureKind.PERMANENT
            ):
                retry_target = state.fallback_url
                effective_failure = failure
            else:
                effective_failure = origin_failure or failure

        state.attempts += 1
        delay = retry_delay_seconds(effective_failure, state.attempts)
        if delay is None:
            self._request_states.pop(index, None)
            memoized_failure = state.origin_failure or effective_failure
            if memoized_failure.code in {"http-404", "http-410"}:
                self._terminal_source_failures[
                    self._failure_identity(state.source_url)
                ] = memoized_failure
            log.info(
                "Thumbnail extraction stopped (%s/%s) for %s after %d attempt(s): %s",
                effective_failure.kind.value,
                effective_failure.code or "unclassified",
                state.source_url,
                state.attempts,
                effective_failure.message,
            )
            self.request_failed.emit(index, effective_failure)
            if pump:
                self._pump()
            return

        log.info(
            "Thumbnail extraction failed (%s/%s) for %s; retrying in %.0fs: %s",
            effective_failure.kind.value,
            effective_failure.code or "unclassified",
            state.source_url,
            delay,
            effective_failure.message,
        )
        version = self._scheduler.version_for(index)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda i=index, v=version, target=retry_target:
                self._retry_request(i, v, target)
        )
        self._retry_timers[index] = timer
        timer.start(max(1, round(delay * 1000)))
        if pump:
            self._pump()

    def _retry_request(
        self,
        index: int,
        version: MediaInfoVersion,
        target: str,
    ) -> None:
        timer = self._retry_timers.pop(index, None)
        if timer is not None:
            timer.deleteLater()
        state = self._request_states.get(index)
        if state is None or not self._scheduler.is_current(version):
            return
        if target == state.source_url:
            state.fallback_url = ""
            state.origin_failure = None
        state.partial_pixmap = None
        state.partial_title = ""
        state.duration_fallback_started = False
        self._enqueue(
            index,
            target,
            state.media_type,
            require_thumbnail=state.require_thumbnail,
            require_title=state.require_title,
            require_duration=state.require_duration,
        )
        self._pump()


# ─────────────────────────────────────────────────────────────────────────────
# MediaInfoService — API baseada em path para inventários de mídia
# ─────────────────────────────────────────────────────────────────────────────

class MediaInfoService(QObject):
    """
    Serviço de extração de info com API baseada em path/url (em vez de índice).
    Usado por superfícies que gerenciam arquivos por path, como a seção de
    downloads da Biblioteca, e não por índice de playlist.

    Sinal:
      info_ready(path, pixmap, title)
    """

    info_ready = Signal(str, QPixmap, str)   # (path, pixmap, title)

    def __init__(
        self,
        queue_factory: Callable[[QObject], MediaInfoQueue],
        parent=None,
        *,
        capacity: int = 256,
    ) -> None:
        super().__init__(parent)
        self._capacity = max(1, int(capacity))
        self._path_to_idx: dict[str, int] = {}
        self._idx_to_path: dict[int, str] = {}
        self._next_idx    = 0
        self._queue = queue_factory(self)
        self._queue.info_ready.connect(self._on_queue_ready)

    def request(
        self,
        path: str,
        media_type: str = "video",
        *,
        require_thumbnail: bool = True,
        require_title: bool = True,
    ):
        """Idempotente: re-emite imediatamente se já extraído."""
        if path in self._path_to_idx:
            idx = self._path_to_idx.pop(path)
            self._path_to_idx[path] = idx
            px, title = self._queue.get_cached(idx)
            thumbnail_ready = not require_thumbnail or (px is not None and not px.isNull())
            title_ready = not require_title or bool(title)
            if px is not None and thumbnail_ready and title_ready:
                self.info_ready.emit(path, px, title)
                return
            if px is not None:
                self._queue.invalidate(idx)
            self._queue.request(
                idx,
                path,
                media_type,
                require_thumbnail=require_thumbnail,
                require_title=require_title,
            )
            return
        idx = self._next_idx; self._next_idx += 1
        self._path_to_idx[path] = idx
        self._idx_to_path[idx]  = path
        self._trim_cache()
        self._queue.request(
            idx,
            path,
            media_type,
            require_thumbnail=require_thumbnail,
            require_title=require_title,
        )

    def get_cached(self, path: str) -> "tuple[QPixmap | None, str]":
        idx = self._path_to_idx.pop(path, None)
        if idx is None:
            return None, ""
        self._path_to_idx[path] = idx
        return self._queue.get_cached(idx)

    def _trim_cache(self) -> None:
        while len(self._path_to_idx) > self._capacity:
            stale_path = next(iter(self._path_to_idx))
            stale_idx = self._path_to_idx.pop(stale_path)
            self._idx_to_path.pop(stale_idx, None)
            self._queue.invalidate(stale_idx)

    def clear(self):
        self._path_to_idx.clear(); self._idx_to_path.clear()
        self._next_idx = 0; self._queue.clear()

    def _on_queue_ready(self, idx: int, pixmap: QPixmap, title: str):
        path = self._idx_to_path.get(idx)
        if path:
            self.info_ready.emit(path, pixmap, title)
