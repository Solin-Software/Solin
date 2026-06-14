"""
media_info_extractor.py  ─ Solin
Extração centralizada de thumbnail + título de mídia, de forma assíncrona
e sem download completo de arquivos remotos.

Fontes de metadado (em ordem de prioridade):
  Áudio local   : bytes brutos do header (ID3v2/MP4 atoms/FLAC/OGG) → sem player
  Áudio remoto  : HTTP Range request (512 KB máx) → mesmos parsers de bytes
  Vídeo local   : QMediaPlayer → QMediaMetaData (CoverArtImage + Title) → frame 5%
  Vídeo remoto  : QMediaPlayer em streaming → mesma lógica (sem download completo)
  URL qualquer  : RemotePageMetaExtractor → og:image + og:title via HTTP HEAD/GET parcial
  Cache local   : qualquer URL com arquivo .done → tratada como local

Sinal principal:  info_ready(index, pixmap, title)
  - pixmap : thumbnail extraída (QPixmap válido) ou QPixmap() se não encontrada
  - title  : título dos metadados, ou "" se não disponível

Para alimentação ao vivo (player em reprodução), use feed_live_frame() /
feed_live_cover() na ThumbnailQueue — emitem info_ready com title="".

Nota: completamente independente da API JW.org. Thumb e título são extraídos
diretamente do stream de mídia (QMediaPlayer) ou dos metadados HTML da página.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable, cast

from PySide6.QtCore import QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink, QMediaMetaData

from ..core.foundation.exception_logging import log_ignored_exception
from ..core.media.info_queue import (
    MediaInfoJob,
    MediaInfoScheduler,
    MediaInfoVersion,
)
from ..core.network.http import HttpError, get as http_get, get_bytes

log = logging.getLogger(__name__)

_DEFAULT_METADATA_READ_BYTES = 512 * 1024
_MAX_ID3_TAG_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers públicos
# ─────────────────────────────────────────────────────────────────────────────

def _get_cached_media_path(
    url: str,
    media_cache_dir: str | os.PathLike[str],
) -> str | None:
    """
    Retorna o caminho local do arquivo se a URL já foi baixada completamente
    (existe o arquivo + marcador .done). Retorna None se não há cache.
    """
    filename = url.split("/")[-1].split("?")[0]
    if not filename:
        return None
    path = os.path.join(os.fspath(media_cache_dir), filename)
    if os.path.exists(path) and os.path.exists(path + ".done"):
        return path
    return None


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


def _audio_info_from_file(path: str) -> "tuple[bytes | None, str]":
    """Read the complete bounded metadata prefix and parse audio information."""
    ext = Path(path).suffix.lower()
    try:
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
# Remote extractors — network I/O in workers, QPixmap creation in GUI thread
# ─────────────────────────────────────────────────────────────────────────────

class _ThreadedRemoteInfoExtractor(QObject):
    info_ready = Signal(int, QPixmap, str)
    thumbnail_failed = Signal(int)

    _worker_ready = Signal(bytes, str)
    _worker_failed = Signal()

    def __init__(self, index: int, url: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._url = url
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        cancelled = self._cancelled
        self.destroyed.connect(lambda *_: cancelled.set())
        self._worker_ready.connect(self._deliver_worker_result)
        self._worker_failed.connect(self._deliver_worker_failure)
        QTimer.singleShot(0, self._start)

    def _start(self) -> None:
        if self._cancelled.is_set():
            return
        thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=type(self).__name__,
        )
        self._thread = thread
        thread.start()

    def cancel(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        self._cancelled.set()
        thread = self._thread
        if (
            wait
            and thread is not None
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, timeout))
        self.deleteLater()

    def _run(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            image_bytes, title = self._fetch_info()
        except Exception as exc:  # noqa: BLE001 - media metadata worker boundary
            log.debug("%s failed for %s: %s", type(self).__name__, self._url, exc)
            self._emit_worker_failure()
            return

        if self._cancelled.is_set():
            return
        try:
            if image_bytes or title:
                self._worker_ready.emit(image_bytes or b"", title)
            else:
                self._worker_failed.emit()
        except RuntimeError:
            return

    def _emit_worker_failure(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            self._worker_failed.emit()
        except RuntimeError:
            return

    def _fetch_info(self) -> tuple[bytes | None, str]:
        raise NotImplementedError

    def _deliver_worker_result(self, image_bytes: bytes, title: str) -> None:
        pixmap = QPixmap()
        if image_bytes and _embedded_image_is_complete(image_bytes):
            pixmap.loadFromData(image_bytes)
        if not pixmap.isNull() or title:
            self.info_ready.emit(self._index, pixmap, title)
        else:
            self.thumbnail_failed.emit(self._index)
        self.deleteLater()

    def _deliver_worker_failure(self) -> None:
        self.thumbnail_failed.emit(self._index)
        self.deleteLater()


class RemoteAudioInfoExtractor(_ThreadedRemoteInfoExtractor):
    """
    Extrai cover art e título de áudio remoto sem baixar o arquivo completo.

    Faz uma requisição HTTP Range (bytes=0–524287, 512 KB máx) para obter
    apenas o header com metadados. Cover e título são lidos em memória pelos
    mesmos parsers usados para arquivos locais (ID3v2, MP4 atoms, FLAC, OGG).
    Nenhum byte além dos necessários é transferido.
    """

    _MAX_BYTES = 3_145_728  # 3 MB (3 * 1024 * 1024)
    _TIMEOUT_S = 10

    def __init__(self, index: int, url: str, parent=None):
        self._ext = Path(url.split("?")[0]).suffix.lower()
        super().__init__(index, url, parent)

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


class RemoteImageInfoExtractor(_ThreadedRemoteInfoExtractor):
    """
    Downloads a remote image thumbnail with a bounded read.

    This is intentionally separate from the media cache: thumbnail files are
    small, derived UI assets and callers decide where/how to persist them.
    """

    _MAX_BYTES = 2 * 1024 * 1024
    _TIMEOUT_S = 12

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


# ─────────────────────────────────────────────────────────────────────────────
# MediaInfoExtractor — QMediaPlayer assíncrono (vídeo local/remoto, áudio local)
# ─────────────────────────────────────────────────────────────────────────────

class MediaInfoExtractor(QObject):
    """
    Extrai thumbnail e título de vídeo/áudio via QMediaPlayer assíncrono.

    Problema clássico do seek resolvido:
      setPosition() é ASSÍNCRONO — monitora positionChanged e só captura o
      frame quando a posição real chegou próxima ao alvo (margem 500ms).

    Prioridade de saída:
      1. Cover art de metadados (QMediaMetaData.CoverArtImage/ThumbnailImage)
      2. Frame capturado no seek de 5% da duração
      Título lido de QMediaMetaData.Title em qualquer dos dois casos.

    Sinais:
      info_ready(index, pixmap, title)  — extração concluída (pixmap pode ser nulo)
      thumbnail_failed(index)           — timeout ou erro irrecuperável
      duration_ready(index, dur_ms)     — duração real lida pelo player
    """

    info_ready       = Signal(int, QPixmap, str)   # (index, pixmap, title)
    thumbnail_failed = Signal(int)
    duration_ready   = Signal(int, int)            # (index, duration_ms)

    _TIMEOUT_MS    = 10_000
    _SEEK_WAIT_MS  =    800
    _COVER_WAIT_MS =    400

    def __init__(self, index: int, url: str, parent=None):
        super().__init__(parent)
        self._index          = index
        self._url            = url
        self._seek_target    = -1
        self._seek_confirmed = False
        self._done           = False
        self._cover_emitted  = False
        self._pending_frame: QPixmap | None = None
        self._meta_title: str = ""          # título lido dos metadados

        self._player = QMediaPlayer(self)
        self._sink   = QVideoSink(self)
        self._player.setVideoSink(self._sink)

        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.metaDataChanged.connect(self._on_metadata)
        self._player.durationChanged.connect(self._on_duration)
        self._player.positionChanged.connect(self._on_position)
        self._player.errorOccurred.connect(self._on_error)

        self._t_global = QTimer(self); self._t_global.setSingleShot(True)
        self._t_global.timeout.connect(self._on_timeout)
        self._t_global.start(self._TIMEOUT_MS)

        self._t_seek = QTimer(self); self._t_seek.setSingleShot(True)
        self._t_seek.timeout.connect(self._accept_any_frame)
        self._t_seek.start(self._SEEK_WAIT_MS)

        self._t_cover = QTimer(self); self._t_cover.setSingleShot(True)
        self._t_cover.timeout.connect(self._emit_pending_frame)

        src = (QUrl(url) if url.startswith(("http://", "https://"))
               else QUrl.fromLocalFile(url))
        self._player.setSource(src)
        self._player.play()

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_metadata(self):
        """Lê cover art e título dos metadados. Cover tem prioridade máxima."""
        if not self._player:
            return

        meta = self._player.metaData()

        # Título — sempre lemos, independente de já ter cover
        if not self._meta_title:
            t = meta.value(QMediaMetaData.Key.Title)
            if isinstance(t, str):
                self._meta_title = t.strip()

        if self._cover_emitted:
            return

        for key in (QMediaMetaData.Key.CoverArtImage, QMediaMetaData.Key.ThumbnailImage):
            value = meta.value(key)
            if value is None:
                continue
            pixmap = None
            if isinstance(value, QImage) and not value.isNull():
                pixmap = QPixmap.fromImage(value)
            elif isinstance(value, QPixmap) and not value.isNull():
                pixmap = value
            if pixmap:
                self._cover_emitted = True
                self._done = True
                self._pending_frame = None
                self.info_ready.emit(self._index, pixmap, self._meta_title)
                QTimer.singleShot(0, self._finish)
                return

    def _on_duration(self, dur_ms: int):
        if self._cover_emitted or self._done:
            return
        if dur_ms > 0 and self._seek_target < 0:
            self._t_seek.stop()
            self.duration_ready.emit(self._index, dur_ms)
            self._seek_target = max(2_000, min(dur_ms * 5 // 100, 10_000))
            self._player.setPosition(self._seek_target)

    def _on_position(self, pos_ms: int):
        if self._seek_confirmed or self._seek_target < 0:
            return
        margin = 500 if self._seek_target > 0 else 0
        if pos_ms >= self._seek_target - margin:
            self._seek_confirmed = True

    def _accept_any_frame(self):
        self._seek_target    = 0
        self._seek_confirmed = True

    def _on_frame(self, frame):
        if self._done or self._cover_emitted or not self._seek_confirmed or not frame.isValid():
            return
        if self._pending_frame is not None:
            return
        img = frame.toImage()
        if img.isNull():
            return
        self._pending_frame = QPixmap.fromImage(img)
        self._t_cover.start(self._COVER_WAIT_MS)

    def _emit_pending_frame(self):
        if self._done or self._cover_emitted:
            return
        px = self._pending_frame
        self._pending_frame = None
        self._done = True
        self._finish()
        if px and not px.isNull():
            self.info_ready.emit(self._index, px, self._meta_title)
        else:
            # Sem frame — mas pode ter título
            if self._meta_title:
                self.info_ready.emit(self._index, QPixmap(), self._meta_title)
            else:
                self.thumbnail_failed.emit(self._index)

    def _on_error(self, _err, _msg):
        if not self._done:
            self._done = True
            self._pending_frame = None
            self._finish()
            self.thumbnail_failed.emit(self._index)

    def _on_timeout(self):
        if not self._done:
            self._done = True
            self._pending_frame = None
            self._finish()
            self.thumbnail_failed.emit(self._index)

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._pending_frame = None
        self._finish()

    def _finish(self):
        self._t_global.stop(); self._t_seek.stop(); self._t_cover.stop()
        self._player.stop()
        self._player.setSource(QUrl())
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


class RemotePageMetaExtractor(_ThreadedRemoteInfoExtractor):
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
            image_bytes = self._fetch_image_bytes(thumb_url) if thumb_url else None
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
    thumbnail_failed = Signal(int)
    duration_ready   = Signal(int, int)

    def __init__(self, index: int, url: str, media_type: str, parent=None):
        super().__init__(parent)
        self._index      = index
        self._url        = url
        self._media_type = media_type
        self._done       = False
        self._stream_ex: MediaInfoExtractor | None = None

        # Estágio 1: tenta og:image + og:title
        self._page_ex = RemotePageMetaExtractor(index, url, self)
        self._page_ex.info_ready.connect(self._on_page_ready)
        self._page_ex.thumbnail_failed.connect(self._on_page_failed)

    def _on_page_ready(self, index: int, pixmap: QPixmap, title: str):
        if self._done:
            return
        self._done = True
        self.info_ready.emit(index, pixmap, title)
        QTimer.singleShot(0, self.deleteLater)

    def _on_page_failed(self, index: int):
        if self._done:
            return
        # Estágio 2: fallback para QMediaPlayer (captura frame do stream)
        self._stream_ex = MediaInfoExtractor(index, self._url, self)
        self._stream_ex.info_ready.connect(self._on_stream_ready)
        self._stream_ex.thumbnail_failed.connect(self._on_stream_failed)
        self._stream_ex.duration_ready.connect(self.duration_ready)

    def _on_stream_ready(self, index: int, pixmap: QPixmap, title: str):
        if self._done:
            return
        self._done = True
        self.info_ready.emit(index, pixmap, title)
        QTimer.singleShot(0, self.deleteLater)

    def _on_stream_failed(self, index: int):
        if self._done:
            return
        self._done = True
        self.thumbnail_failed.emit(index)
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


ExtractorFactory = Callable[[int, str, str, QObject], QObject]


def _remote_image_factory(index: int, url: str, _media_type: str, parent: QObject) -> QObject:
    return RemoteImageInfoExtractor(index, url, parent)


def _remote_audio_factory(index: int, url: str, _media_type: str, parent: QObject) -> QObject:
    return RemoteAudioInfoExtractor(index, url, parent)


def _remote_video_factory(index: int, url: str, media_type: str, parent: QObject) -> QObject:
    return _RemoteVideoMetaThenStream(index, url, media_type, parent)


def _local_media_factory(index: int, url: str, _media_type: str, parent: QObject) -> QObject:
    return MediaInfoExtractor(index, url, parent)


_EXTRACTOR_FACTORIES: dict[tuple[bool, str | None], ExtractorFactory] = {
    (True, "image"): _remote_image_factory,
    (True, "audio"): _remote_audio_factory,
    (True, None): _remote_video_factory,
    (False, None): _local_media_factory,
}


def _create_extractor(index: int, url: str, media_type: str, parent: QObject) -> QObject:
    is_remote = url.startswith(("http://", "https://"))
    factory = _EXTRACTOR_FACTORIES.get((is_remote, media_type))
    if factory is None:
        factory = _EXTRACTOR_FACTORIES[(is_remote, None)]
    return factory(index, url, media_type, parent)


class MediaInfoQueue(QObject):
    """
    Gerencia a extração de thumbnail + título em fila, com no máximo
    _MAX_CONCURRENT extratores simultâneos.

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

    _MAX_CONCURRENT = 2

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        thumb_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._thumb_cache_dir = os.fspath(thumb_cache_dir)
        self._scheduler = MediaInfoScheduler(
            max_concurrent=self._MAX_CONCURRENT,
        )
        self._cache: dict[int, tuple[QPixmap, str]] = {}
        self._extractors: dict[int, QObject] = {}

    # ── Disk Cache ────────────────────────────────────────────────────────────

    def _get_cache_paths(self, url: str) -> "tuple[str, str]":
        """Retorna caminhos absolutos para a imagem (.jpg) e metadados (.json) cacheados."""
        if not url.startswith(("http://", "https://")):
            url_to_hash = os.path.normcase(os.path.abspath(url))
        else:
            url_to_hash = url
        h = hashlib.md5(url_to_hash.encode("utf-8")).hexdigest()
        base_dir = os.path.join(self._thumb_cache_dir, "extracted")
        base_path = os.path.join(base_dir, h)
        return f"{base_path}.jpg", f"{base_path}.json"

    def _load_from_disk_cache(self, url: str) -> "tuple[QPixmap | None, str]":
        img_path, meta_path = self._get_cache_paths(url)
        
        # O JSON é o nosso marker de cache
        if not os.path.exists(meta_path):
            return None, ""
        
        title = ""
        has_thumb = False
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                title = data.get("title", "")
                has_thumb = data.get("has_thumb", True)
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            log.debug("Could not read media info cache %s: %s", meta_path, exc)
            
        px = QPixmap()
        if has_thumb and os.path.exists(img_path):
            px = QPixmap(img_path)
            
        return px, title

    def _save_to_disk_cache(self, url: str, pixmap: QPixmap, title: str):
        img_path, meta_path = self._get_cache_paths(url)
        try:
            os.makedirs(os.path.dirname(img_path), exist_ok=True)
            has_thumb = pixmap is not None and not pixmap.isNull()
            if has_thumb:
                pixmap.save(img_path, "JPG", quality=90)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"title": title, "has_thumb": has_thumb}, f, ensure_ascii=False)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            log.debug("Could not save media info cache for %s: %s", url, exc)

    # ── API pública ───────────────────────────────────────────────────────────

    def request(self, index: int, url: str, media_type: str = "video"):
        """Solicita extração de info para o item. Idempotente."""
        if index in self._cache or self._scheduler.is_scheduled(index):
            return

        # Fast-Path: Tenta carregar do cache de disco ANTES de qualquer coisa
        disk_px, disk_title = self._load_from_disk_cache(url)
        if disk_px is not None:
            self._cache[index] = (disk_px, disk_title)
            self._emit_info_later(
                self._scheduler.version_for(index),
                index,
                disk_px,
                disk_title,
            )
            return

        is_remote = url.startswith(("http://", "https://"))

        # ── Imagem ──────────────────────────────────────────────────────────
        if media_type == "image":
            target = (
                _get_cached_media_path(url, self._media_cache_dir)
                if is_remote
                else url
            )
            if target:
                px = QPixmap(target)
                self._cache[index] = (px if not px.isNull() else QPixmap(), "")
                if not px.isNull():
                    self._emit_info_later(
                        self._scheduler.version_for(index),
                        index,
                        px,
                        "",
                    )
                return
            if is_remote:
                self._enqueue(index, url, media_type)
                self._pump()
                return
            self._cache[index] = (QPixmap(), "")
            return

        # ── Áudio local — bytes brutos (zero player, zero thread) ───────────
        if media_type == "audio" and not is_remote:
            cover_bytes, title = _audio_info_from_file(url)
            if cover_bytes and _embedded_image_is_complete(cover_bytes):
                px = QPixmap()
                if px.loadFromData(cover_bytes) and not px.isNull():
                    self._save_to_disk_cache(url, px, title)
                    self._cache[index] = (px, title)
                    self._emit_info_later(
                        self._scheduler.version_for(index),
                        index,
                        px,
                        title,
                    )
                    return
            # Sem cover nos bytes brutos → extrator (tenta QMediaMetaData)

        # ── Remoto: verifica cache local antes de qualquer rede ─────────────
        if is_remote:
            cached_path = _get_cached_media_path(url, self._media_cache_dir)
            target = cached_path if cached_path else url
            self._enqueue(index, target, media_type)
            self._pump()
            return

        # ── Local (vídeo ou áudio sem cover nos bytes) → fila ───────────────
        self._enqueue(index, url, media_type)
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
        for job in self._scheduler.invalidate(index):
            self._cancel_job(job)
        self._cache.pop(index, None)

    def clear(self):
        active_jobs = self._scheduler.clear()
        self._cache.clear()
        for job in active_jobs:
            self._cancel_job(job)

    def shutdown(self, timeout: float = 2.0) -> None:
        active_jobs = self._scheduler.shutdown()
        self._cache.clear()
        deadline = time.monotonic() + max(0.0, timeout)
        for job in active_jobs:
            remaining = max(0.0, deadline - time.monotonic())
            self._cancel_job(job, wait=True, timeout=remaining)

    # ── Interno ───────────────────────────────────────────────────────────────

    def _enqueue(self, index: int, url: str, media_type: str) -> None:
        self._scheduler.enqueue(index, url, media_type)

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
                self,
            )
        except Exception:  # noqa: BLE001 - Qt factory boundary isolates one failed job
            self._scheduler.fail(job, job.index)
            log.exception(
                "Could not create media info extractor for %s",
                job.url,
            )
            self._save_to_disk_cache(job.url, QPixmap(), "")
            self._cache[job.index] = (QPixmap(), "")
            return
        self._extractors[job.index] = ex
        extractor_signals = cast(Any, ex)
        extractor_signals.info_ready.connect(
            lambda index, pixmap, title, current=job:
                self._on_ready(current, index, pixmap, title)
        )
        extractor_signals.thumbnail_failed.connect(
            lambda index, current=job:
                self._on_failed(current, index)
        )
        duration_ready = getattr(ex, "duration_ready", None)
        if duration_ready is not None:
            duration_ready.connect(
                lambda index, dur_ms, current=job:
                    self._on_duration_ext(current, index, dur_ms)
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
        if self._scheduler.accepts_result(job, index) and dur_ms > 0:
            self.duration_ready.emit(index, dur_ms)

    def _on_ready(
        self,
        job: MediaInfoJob,
        index: int,
        pixmap: QPixmap,
        title: str,
    ):
        if not self._scheduler.complete(job, index):
            return
        self._extractors.pop(index, None)
        self._save_to_disk_cache(job.url, pixmap, title)
        self._cache[index] = (pixmap, title)
        self.info_ready.emit(index, pixmap, title)
        self._pump()

    def _on_failed(self, job: MediaInfoJob, index: int):
        if not self._scheduler.fail(job, index):
            return
        self._extractors.pop(index, None)
        self._save_to_disk_cache(job.url, QPixmap(), "")
        self._cache[index] = (QPixmap(), "")
        self._pump()


# ─────────────────────────────────────────────────────────────────────────────
# MediaInfoService — API baseada em path (para CacheManagerWidget, etc.)
# ─────────────────────────────────────────────────────────────────────────────

class MediaInfoService(QObject):
    """
    Serviço de extração de info com API baseada em path/url (em vez de índice).
    Usado por widgets que gerenciam arquivos por path (CacheManagerWidget,
    CacheMediaWidget) e não por índice de playlist.

    Sinal:
      info_ready(path, pixmap, title)
    """

    info_ready = Signal(str, QPixmap, str)   # (path, pixmap, title)

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        thumb_cache_dir: str | os.PathLike[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path_to_idx: dict[str, int] = {}
        self._idx_to_path: dict[int, str] = {}
        self._next_idx    = 0
        self._queue = MediaInfoQueue(media_cache_dir, thumb_cache_dir, self)
        self._queue.info_ready.connect(self._on_queue_ready)

    def request(self, path: str, media_type: str = "video"):
        """Idempotente: re-emite imediatamente se já extraído."""
        if path in self._path_to_idx:
            idx = self._path_to_idx[path]
            px, title = self._queue.get_cached(idx)
            if px is not None and not px.isNull():
                self.info_ready.emit(path, px, title)
            return
        idx = self._next_idx; self._next_idx += 1
        self._path_to_idx[path] = idx
        self._idx_to_path[idx]  = path
        self._queue.request(idx, path, media_type)

    def get_cached(self, path: str) -> "tuple[QPixmap | None, str]":
        idx = self._path_to_idx.get(path)
        if idx is None:
            return None, ""
        return self._queue.get_cached(idx)

    def clear(self):
        self._path_to_idx.clear(); self._idx_to_path.clear()
        self._next_idx = 0; self._queue.clear()

    def _on_queue_ready(self, idx: int, pixmap: QPixmap, title: str):
        path = self._idx_to_path.get(idx)
        if path:
            self.info_ready.emit(path, pixmap, title)
