"""
writer.py
─────────────────────
Escreve arquivos .jwlplaylist compatíveis com JW Library ≥ 14.

Formato de saída:
  • ZIP contendo userData.db (SQLite) + mídias embutidas (se houver)

Suporta:
  • Itens de vídeo/áudio do JW.org → tabela Location + Tag/TagMap
  • Arquivos locais (vídeo, imagem, áudio) → tabela IndependentMedia + arquivo embutido no ZIP

BUGS CORRIGIDOS:
  1. Regex de URL reescritas para cobrir os formatos reais do JW CDN:
       - sjjm_T_002_r720P.mp4   (qualidade com prefixo 'r')
       - osg_T_108.mp3          (sem sufixo de qualidade)
       - pub-sjjm_T_1_r720P     (formato pub- antigo)
  2. Tabelas Tag + TagMap criadas e preenchidas (sem elas o JW Library não abre).
  3. BaseDurationTicks na PlaylistItemLocationMap preservado / estimado.
  4. ThumbnailFilePath definido para itens com IndependentMedia.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
import uuid
import zipfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Resolução de metadados JW.org ────────────────────────────────────────────
from solin.core.jw.identifiers import is_jw_url
from solin.core.jw.metadata import resolve_jworg_meta
from solin.core.media.download_storage import completed_cached_path

from solin.core.media.jw_reference import parse_jw_media_reference
from .schema import create_jwlplaylist_schema


def _mp4_read_title(data: bytes) -> Optional[str]:
    """
    Extrai o título (átomo ©nam / iTunes) de bytes MP4/M4A/MOV.
    Navega: moov → udta → meta (FullBox) → ilst → ©nam → data

    Retorna None se o arquivo não tiver metadado de título.
    """
    size = len(data)

    def _boxes(start: int, end: int):
        """Gera (type_bytes, inner_start, box_end) para cada box em [start, end)."""
        pos = start
        while pos + 8 <= end:
            try:
                bsz  = struct.unpack_from(">I", data, pos)[0]
                btyp = data[pos + 4: pos + 8]
                if bsz == 1:                          # extended size (64-bit)
                    if pos + 16 > end: return
                    bsz   = struct.unpack_from(">Q", data, pos + 8)[0]
                    inner = pos + 16
                elif bsz == 0:                        # last box até fim
                    bsz   = end - pos
                    inner = pos + 8
                else:
                    inner = pos + 8
                if bsz < 8: return
                box_end = pos + bsz
                yield btyp, inner, box_end
                pos = box_end
            except (struct.error, IndexError):
                return

    def _find(start: int, end: int, target: bytes):
        for btyp, inner, box_end in _boxes(start, end):
            if btyp == target:
                return inner, box_end
        return -1, -1

    moov_s, moov_e = _find(0, size, b'moov')
    if moov_s < 0: return None
    udta_s, udta_e = _find(moov_s, moov_e, b'udta')
    if udta_s < 0: return None
    meta_s, meta_e = _find(udta_s, udta_e, b'meta')
    if meta_s < 0: return None
    # meta é um FullBox: 4 bytes (version + flags) antes dos filhos
    ilst_s, ilst_e = _find(meta_s + 4, meta_e, b'ilst')
    if ilst_s < 0: return None
    # ©nam = 0xa9 'n' 'a' 'm'
    cnam_s, cnam_e = _find(ilst_s, ilst_e, b'\xa9nam')
    if cnam_s < 0: return None
    dbox_s, dbox_e = _find(cnam_s, cnam_e, b'data')
    if dbox_s < 0: return None
    # data FullBox: 4 bytes tipo (well-known type) + 4 bytes locale → depois UTF-8
    text_start = dbox_s + 8
    if text_start >= dbox_e: return None
    raw_text = data[text_start:dbox_e]
    try:
        return raw_text.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return raw_text.decode("latin-1", errors="replace").strip() or None


def _mp3_read_title(data: bytes) -> Optional[str]:
    """
    Extrai o título (frame TIT2) de tags ID3v2 em bytes MP3.
    Suporta ID3v2.3 e ID3v2.4 (os formatos mais comuns).
    Retorna None se não encontrar.
    """
    if len(data) < 10 or data[:3] != b"ID3":
        return None

    version_major = data[3]
    if version_major < 3:
        return None   # ID3v2.2 usa frame IDs de 3 bytes — não suportado

    # Tamanho da tag (syncsafe integer — bit 7 de cada byte é zero)
    raw = data[6:10]
    tag_size = (
        (raw[0] & 0x7F) << 21 |
        (raw[1] & 0x7F) << 14 |
        (raw[2] & 0x7F) <<  7 |
         raw[3] & 0x7F
    )

    pos = 10
    end = min(10 + tag_size, len(data))

    while pos + 10 <= end:
        frame_id  = data[pos: pos + 4]
        if frame_id == b"\x00\x00\x00\x00":
            break
        frame_sz  = struct.unpack_from(">I", data, pos + 4)[0]
        pos      += 10
        if frame_sz <= 0:
            break
        frame_end = min(pos + frame_sz, end)

        if frame_id == b"TIT2" and frame_sz > 1:
            enc  = data[pos]
            text = data[pos + 1: frame_end]
            try:
                if enc == 1:
                    return text.decode("utf-16").strip("\x00").strip() or None
                elif enc == 2:
                    return text.decode("utf-16-be").strip("\x00").strip() or None
                elif enc == 3:
                    return text.decode("utf-8").strip("\x00").strip() or None
                else:       # enc == 0: ISO-8859-1
                    return text.decode("latin-1").strip("\x00").strip() or None
            except (UnicodeDecodeError, LookupError):
                pass

        pos = frame_end

    return None


def _read_label_from_local(data: bytes, ext: str) -> Optional[str]:
    """
    Tenta extrair o título oficial dos metadados de um arquivo de mídia local.
    Suporta MP4/M4A/MOV (iTunes ©nam) e MP3 (ID3v2 TIT2).
    Retorna None se não encontrar ou se o formato não for suportado.
    """
    if not data:
        return None
    try:
        if ext in (".mp4", ".m4a", ".mov", ".m4v"):
            return _mp4_read_title(data)
        if ext == ".mp3":
            return _mp3_read_title(data)
    except (struct.error, IndexError, TypeError, UnicodeError, ValueError) as exc:
        log.debug("[writer] Failed to read title metadata (%s): %s", ext, exc)
    return None

_MIME_FROM_EXT: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".mkv":  "video/x-matroska",
    ".mov":  "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".ogg":  "audio/ogg",
    ".wav":  "audio/wav",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}

# Mapa reverso: mime_type → extensão
_MIME_FROM_EXT_REV: dict[str, str] = {v: k for k, v in _MIME_FROM_EXT.items()}

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Leitura de duração de arquivos de mídia locais ────────────────────────────
#
# Usado como fallback no export quando base_duration_ticks não foi preenchido
# pelo player (item adicionado mas nunca reproduzido).
#
# Suporte:
#   MP4 / MOV / M4A / M4V  →  box moov/mvhd
#   WebM / MKV              →  EBML Segment/Info/Duration
# Outros formatos retornam 0 (sem dependências externas).


def _mp4_duration_ms(data: bytes) -> int:
    """Busca o box mvhd dentro do box moov e retorna duração em ms. 0 se falhar."""
    size = len(data)

    def _scan(start: int, end: int) -> int:
        pos = start
        while pos + 8 <= end:
            try:
                bsize = struct.unpack_from(">I", data, pos)[0]
                btype = data[pos + 4: pos + 8]
                if bsize == 1:
                    if pos + 16 > end:
                        break
                    bsize = struct.unpack_from(">Q", data, pos + 8)[0]
                    inner = pos + 16
                elif bsize == 0:
                    bsize = end - pos  # last box
                    inner = pos + 8
                else:
                    inner = pos + 8
                if bsize < 8:
                    break
                box_end = pos + bsize
                if btype == b"moov":
                    result = _scan(inner, min(box_end, end))
                    if result:
                        return result
                elif btype == b"mvhd":
                    ver = data[inner] if inner < end else 0
                    if ver == 1:
                        if inner + 32 > end:
                            break
                        ts = struct.unpack_from(">I", data, inner + 20)[0]
                        dur = struct.unpack_from(">Q", data, inner + 24)[0]
                    else:
                        if inner + 20 > end:
                            break
                        ts = struct.unpack_from(">I", data, inner + 12)[0]
                        dur = struct.unpack_from(">I", data, inner + 16)[0]
                    if ts > 0:
                        return int(dur * 1000 / ts)
                pos = box_end
            except (struct.error, IndexError):
                break
        return 0

    return _scan(0, size)


def _ebml_read_vint(data: bytes, pos: int) -> tuple[int, int]:
    """Lê um VINT do EBML. Retorna (value, bytes_consumed)."""
    if pos >= len(data):
        return 0, 0
    b = data[pos]
    if b == 0:
        return 0, 0
    width = 1
    mask = 0x80
    while not (b & mask):
        width += 1
        mask >>= 1
        if width > 8:
            return 0, 0
    val = b & (mask - 1)
    for i in range(1, width):
        if pos + i >= len(data):
            return 0, 0
        val = (val << 8) | data[pos + i]
    return val, width


def _webm_duration_ms(data: bytes) -> int:
    """
    Busca o elemento Duration no bloco Info do Segment (WebM/MKV).
    Retorna duração em ms. 0 se falhar.
    """
    SEGMENT_ID   = 0x18538067
    INFO_ID      = 0x1549A966
    DURATION_ID  = 0x4489
    TIMESCALE_ID = 0x2AD7B1

    size = len(data)
    timescale_ns = 1_000_000  # default MKV: 1ms per tick

    def _read_element_id(pos: int) -> tuple[int, int]:
        """Retorna (element_id, bytes_consumed)."""
        if pos >= size:
            return 0, 0
        b = data[pos]
        if b & 0x80:
            return b, 1
        if b & 0x40:
            if pos + 1 >= size:
                return 0, 0
            return ((b & 0x3F) << 8) | data[pos + 1], 2
        if b & 0x20:
            if pos + 2 >= size:
                return 0, 0
            return (((b & 0x1F) << 16) | (data[pos + 1] << 8) | data[pos + 2]), 3
        if b & 0x10:
            if pos + 3 >= size:
                return 0, 0
            return (((b & 0x0F) << 24) | (data[pos + 1] << 16)
                    | (data[pos + 2] << 8) | data[pos + 3]), 4
        return 0, 0

    def _scan(start: int, end: int, target_ids: set) -> dict:
        """Varre elementos EBML e retorna {id: bytes_data} para os IDs alvo."""
        pos = start
        found: dict[int, bytes] = {}
        while pos + 2 <= end:
            eid, eid_sz = _read_element_id(pos)
            if not eid_sz:
                break
            pos += eid_sz
            esize, esz = _ebml_read_vint(data, pos)
            if not esz:
                break
            pos += esz
            if eid in target_ids:
                found[eid] = data[pos: pos + esize]
            if len(found) == len(target_ids):
                break
            pos += esize
        return found

    # Localiza Segment (top-level)
    pos = 0
    while pos + 4 <= size:
        eid, esz = _read_element_id(pos)
        if not esz:
            break
        pos += esz
        dsize, dsz = _ebml_read_vint(data, pos)
        if not dsz:
            break
        pos += dsz
        if eid == SEGMENT_ID:
            seg_start = pos
            seg_end   = pos + dsize if dsize < (1 << 56) else size
            # Dentro do Segment, busca Info
            info_found = _scan(seg_start, min(seg_end, size), {INFO_ID})
            if INFO_ID in info_found:
                info_data = info_found[INFO_ID]
                # Dentro de Info, busca TimecodeScale e Duration
                inner = _scan(0, len(info_data), {TIMESCALE_ID, DURATION_ID})
                if TIMESCALE_ID in inner and len(inner[TIMESCALE_ID]) >= 1:
                    ts_bytes = inner[TIMESCALE_ID]
                    ts_val = 0
                    for b in ts_bytes:
                        ts_val = (ts_val << 8) | b
                    if ts_val > 0:
                        timescale_ns = ts_val
                if DURATION_ID in inner and len(inner[DURATION_ID]) in (4, 8):
                    dur_bytes = inner[DURATION_ID]
                    fmt = ">f" if len(dur_bytes) == 4 else ">d"
                    dur_ticks = struct.unpack(fmt, dur_bytes)[0]
                    # dur_ticks está em unidades de timescale_ns nanosegundos
                    return int(dur_ticks * timescale_ns / 1_000_000)
            break
        pos += dsize

    return 0


def _read_duration_ms_from_bytes(data: bytes, ext: str) -> int:
    """
    Tenta extrair a duração em ms de bytes de arquivo de mídia local.
    Suporta MP4/MOV/M4A/M4V e WebM/MKV. Retorna 0 em caso de falha.
    """
    if not data:
        return 0
    try:
        if ext in (".mp4", ".mov", ".m4a", ".m4v"):
            return _mp4_duration_ms(data)
        if ext in (".webm", ".mkv"):
            return _webm_duration_ms(data)
    except (struct.error, IndexError, TypeError, ValueError):
        log.debug("Failed to read media duration from embedded bytes", exc_info=True)
    return 0


def _new_uuid() -> str:
    """Gera um UUID v4 como string (sem hífens em alguns campos, com em outros)."""
    return str(uuid.uuid4())


class PlaylistWriteError(RuntimeError):
    """The playlist could not be serialized to a JW Library archive."""


def _write_jwlplaylist(
    playlist_name: str,
    items: list[dict],
    output_path: str | Path,
    media_cache_dir: str | os.PathLike[str],
    fallback_lang_code: str = "E",
) -> None:
    """
    Escreve um arquivo .jwlplaylist compatível com JW Library ≥ 14.

    do que foi passado pelo chamador.
    do que foi passado pelo chamador.

      • Itens JW.org  → usa Location + PlaylistItemLocationMap (referência canônica).
                        Consulta a API pub-media para título oficial e duração.
                        NUNCA embute o arquivo de mídia JW — apenas a referência.
      • Arquivos locais (vídeo/áudio/imagem) → embute no ZIP via IndependentMedia.
        - título resolvido dos metadados do arquivo (©nam / ID3v2 TIT2)
        - para imagens: ThumbnailFilePath = o próprio arquivo embarcado
        - para vídeo/áudio: thumbnail separado se disponível
      • URL remota em cache local → equivalente a arquivo local (usa o arquivo em cache)
      • URL remota sem cache e sem key_symbol → não pode ser exportada; item inserido
        sem mídia associada (o JW Library mostrará como item vazio/inválido)

    Parâmetros por item em `items`:
      title         : str  – usado como fallback se não resolvido
      url           : str  – URL http(s) do JW CDN, URL genérica, ou caminho local
      type          : "video" | "audio" | "image"
      key_symbol    : str  | None
      track         : int  | None
      issue_tag     : int  | None
      doc_id        : int  | None
      meps_language : int  (ID MEPS, ex: 5 = pt_BR)

    fallback_lang_code: código de língua JW.org (ex: "T") usado como fallback
    quando meps_language não está no mapa interno.
    """
    output_path = Path(output_path)

    con = sqlite3.connect(":memory:")
    _create_schema(con)

    embedded_files: list[tuple[str, bytes]] = []  # (zip_path, data)

    location_id_seq  = 1
    ind_media_id_seq = 1
    playlist_item_id = 1

    tag_id = 1
    con.execute(
        "INSERT INTO Tag (TagId, Type, Name) VALUES (?, 2, ?)",
        (tag_id, playlist_name),
    )
    tag_map_id = 1

    for position, item in enumerate(items):
        title      = item.get("title", f"Item {position + 1}")
        url        = item.get("url") or ""
        key_symbol = item.get("key_symbol")
        track      = item.get("track")
        issue_tag  = item.get("issue_tag")
        doc_id     = item.get("doc_id")
        meps_lang  = item.get("meps_language") or item.get("language") or 0
        item_type  = item.get("type", "video")

        start_trim = item.get("start_trim_ticks")
        end_trim   = item.get("end_trim_ticks")
        accuracy   = item.get("accuracy", 1)
        end_action = item.get("end_action", 0)
        base_duration = item.get("base_duration_ticks")

        thumbnail_path = None

        # ── PASSO 1: tenta extrair key_symbol/doc_id da URL se não fornecido ──
        # parse_jw_media_reference funciona para URLs JW e caminhos locais como
        # "C:/Downloads/rr_T_43.mp3".
        # original_filename: nome antes de ser renomeado para hash (ex: via WiFi).
        orig_filename = item.get("original_filename", "")
        if not key_symbol and url:
            parsed = parse_jw_media_reference(
                url,
                original_filename=orig_filename,
            )
            if parsed:
                key_symbol = parsed.get("key_symbol")   # pode ser None (docid-only)
                track      = parsed.get("track",    track)
                issue_tag  = parsed.get("issue_tag", issue_tag)
                doc_id     = parsed.get("doc_id",    doc_id)
                if not meps_lang:
                    meps_lang = parsed.get("meps_language", 0)

        # ── PASSO 2: determina se é item JW.org (exporta como referência) ────
        # JW.org quando tem key_symbol (publicação simbólica) OU doc_id de CDN JW.
        # Arquivos locais com nome JW (ex: rr_T_43.mp3) TAMBÉM são JW quando
        # parse_jw_media_reference extraiu um key_symbol/doc_id deles.
        is_jworg = bool(key_symbol) or bool(
            doc_id and url and is_jw_url(url)
        )

        # ── PASSO 3: para URLs http, verifica cache local ─────────────────────
        # Permite usar o arquivo cacheado para duração/metadados mesmo em JW.org.
        # Para não-JW, permite tratar URLs remotas baixadas como arquivos locais.
        is_http = url.startswith(("http://", "https://"))
        local_cached_path: Optional[str] = None
        if is_http:
            local_cached_path = completed_cached_path(url, media_cache_dir)

        # ─────────────────────────────────────────────────────────────────────
        # RAMO A: Item JW.org → Location + PlaylistItemLocationMap
        # Nunca embute o arquivo de mídia — apenas a referência canônica.
        # ─────────────────────────────────────────────────────────────────────
        if is_jworg:
            mmt = item.get("major_multimedia_type")
            if mmt is None:
                mmt = 0 if item_type == "audio" else 2

            # Consulta API JW.org para título canônico e duração
            jw_meta = resolve_jworg_meta(
                key_symbol            = key_symbol,
                doc_id                = doc_id,
                track                 = track,
                issue_tag             = issue_tag,
                meps_language         = meps_lang,
                fallback_lang         = fallback_lang_code,
                major_multimedia_type = mmt,
            )
            if jw_meta:
                if jw_meta.get("title"):
                    title = jw_meta["title"]
                if not base_duration and jw_meta.get("duration_ticks"):
                    base_duration = jw_meta["duration_ticks"]

            # Fallback de duração: arquivo em cache local (item baixado mas API offline)
            if not base_duration and local_cached_path:
                try:
                    ext_c = Path(local_cached_path).suffix.lower()
                    dur_ms = _read_duration_ms_from_bytes(
                        Path(local_cached_path).read_bytes(), ext_c
                    )
                    if dur_ms:
                        base_duration = dur_ms * 10_000
                except OSError:
                    log.debug(
                        "Failed to read fallback duration from cached file %s",
                        local_cached_path,
                        exc_info=True,
                    )

            # Location.Title deve ser vazio (padrão real do JW Library)
            con.execute(
                """INSERT INTO Location
                   (LocationId, BookNumber, ChapterNumber, DocumentId, Track,
                    IssueTagNumber, KeySymbol, MepsLanguage, Type, Title)
                   VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, 3, '')""",
                (location_id_seq, doc_id, track, issue_tag or 0,
                 key_symbol, meps_lang),
            )
            con.execute(
                """INSERT INTO PlaylistItemLocationMap
                   (PlaylistItemId, LocationId, MajorMultimediaType, BaseDurationTicks)
                   VALUES (?, ?, ?, ?)""",
                (playlist_item_id, location_id_seq, mmt, base_duration),
            )
            location_id_seq += 1

            # Thumbnail: embute se disponível (sem extensão — padrão JW Library)
            thumb_data = item.get("thumbnail_data")
            if thumb_data:
                t_file_uuid = _new_uuid()
                t_orig_uuid = _new_uuid()
                t_hash = _sha256(thumb_data)
                con.execute(
                    """INSERT INTO IndependentMedia
                       (IndependentMediaId, OriginalFilename, FilePath, MimeType, Hash)
                       VALUES (?, ?, ?, 'image/jpeg', ?)""",
                    (ind_media_id_seq, t_orig_uuid, t_file_uuid, t_hash),
                )
                embedded_files.append((t_file_uuid, thumb_data))
                thumbnail_path = t_file_uuid
                ind_media_id_seq += 1
            else:
                thumbnail_path = item.get("thumbnail_file_path")

        # ─────────────────────────────────────────────────────────────────────
        # RAMO B: Arquivo local OU URL remota com cache local → embute no ZIP
        # ─────────────────────────────────────────────────────────────────────
        elif item.get("data") or (url and not is_http) or local_cached_path:
            # Resolve o caminho/dados do arquivo
            if item.get("data"):
                # Bytes já em memória (item importado via reader)
                file_data = item["data"]
                mime_type = item.get("mime_type") or "application/octet-stream"
                orig_name = item.get("filename") or f"media_{playlist_item_id}"
                ext       = Path(orig_name).suffix.lower()
                if not ext:
                    ext = _MIME_FROM_EXT_REV.get(mime_type, "")
            elif local_cached_path:
                # URL remota cacheada → usa arquivo local
                lp        = Path(local_cached_path)
                file_data = lp.read_bytes()
                ext       = lp.suffix.lower()
                orig_name = lp.name
                mime_type = _MIME_FROM_EXT.get(ext, "application/octet-stream")
            else:
                # Caminho local direto
                lp = Path(url)
                if not lp.exists():
                    log.warning(
                        "[writer] Local file not found: '%s' - item ignored.", url
                    )
                    playlist_item_id += 1
                    continue
                file_data = lp.read_bytes()
                ext       = lp.suffix.lower()
                orig_name = lp.name
                mime_type = _MIME_FROM_EXT.get(ext, "application/octet-stream")

            # ── Corrige mime_type pela extensão real ───────────────────────────
            # Evita "imagem exportada como vídeo" quando item_type está errado.
            # A extensão do arquivo é a fonte de verdade para o mime_type.
            if ext in _MIME_FROM_EXT:
                detected_mime = _MIME_FROM_EXT[ext]
                if detected_mime != mime_type:
                    log.debug(
                        "[writer] mime_type corrected: %r -> %r (extension %r)",
                        mime_type, detected_mime, ext,
                    )
                    mime_type = detected_mime

            is_image = mime_type.startswith("image/")

            # ── Resolve título dos metadados do arquivo ───────────────────────
            if not is_image:
                resolved = _read_label_from_local(file_data, ext)
                if resolved:
                    title = resolved

            # FilePath no ZIP: UUID + extensão (padrão JW Library)
            file_hash = _sha256(file_data)
            zip_uuid  = _new_uuid()
            zip_name  = f"{zip_uuid}{ext}"

            con.execute(
                """INSERT INTO IndependentMedia
                   (IndependentMediaId, OriginalFilename, FilePath, MimeType, Hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (ind_media_id_seq, orig_name, zip_name, mime_type, file_hash),
            )

            # Duração: 0 para imagens; real para vídeo/áudio
            if is_image:
                duration_ticks = 0
            elif base_duration:
                duration_ticks = base_duration
            else:
                dur_ms = _read_duration_ms_from_bytes(file_data, ext)
                duration_ticks = dur_ms * 10_000  # ms → 100ns ticks

            con.execute(
                """INSERT INTO PlaylistItemIndependentMediaMap
                   (PlaylistItemId, IndependentMediaId, DurationTicks)
                   VALUES (?, ?, ?)""",
                (playlist_item_id, ind_media_id_seq, duration_ticks),
            )
            embedded_files.append((zip_name, file_data))

            # Thumbnail
            if is_image:
                # Para imagens, o próprio arquivo embutido é a thumbnail
                thumbnail_path = zip_name
            else:
                # Para vídeo/áudio: embute thumbnail separado se disponível
                thumb_data = item.get("thumbnail_data")
                if thumb_data:
                    t_file_uuid = _new_uuid()
                    t_orig_uuid = _new_uuid()
                    t_hash = _sha256(thumb_data)
                    con.execute(
                        """INSERT INTO IndependentMedia
                           (IndependentMediaId, OriginalFilename, FilePath, MimeType, Hash)
                           VALUES (?, ?, ?, 'image/jpeg', ?)""",
                        (ind_media_id_seq + 1, t_orig_uuid, t_file_uuid, t_hash),
                    )
                    embedded_files.append((t_file_uuid, thumb_data))
                    thumbnail_path = t_file_uuid
                    ind_media_id_seq += 1
                else:
                    thumbnail_path = item.get("thumbnail_file_path")

            ind_media_id_seq += 1

        # ─────────────────────────────────────────────────────────────────────
        # RAMO C: URL remota sem cache e sem referência JW → não exportável
        # ─────────────────────────────────────────────────────────────────────
        else:
            log.warning(
                "[writer] Item '%s' (pos=%d) could not be exported: "
                "remote URL has no local cache and no JW.org reference. "
                "url=%r  key_symbol=%r  doc_id=%r",
                title, position, url, key_symbol, doc_id,
            )
            # Insere PlaylistItem sem mídia — JW Library mostrará como item vazio.

        # ── Insere PlaylistItem ───────────────────────────────────────────────
        con.execute(
            """INSERT INTO PlaylistItem
               (PlaylistItemId, Label, StartTrimOffsetTicks, EndTrimOffsetTicks,
                Accuracy, EndAction, ThumbnailFilePath)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (playlist_item_id, title, start_trim, end_trim,
             accuracy, end_action, thumbnail_path),
        )

        con.execute(
            """INSERT INTO TagMap
               (TagMapId, PlaylistItemId, LocationId, NoteId, TagId, Position)
               VALUES (?, ?, NULL, NULL, ?, ?)""",
            (tag_map_id, playlist_item_id, tag_id, position),
        )
        tag_map_id += 1
        playlist_item_id += 1

    con.commit()

    db_bytes = _serialize_db(con)
    con.close()

    manifest = _build_manifest(playlist_name, db_bytes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("userData.db",   db_bytes)
        zf.writestr("default_thumbnail.png", _DEFAULT_THUMBNAIL_PNG)
        for zip_path, data in embedded_files:
            zf.writestr(zip_path, data)


def write_jwlplaylist(
    playlist_name: str,
    items: list[dict],
    output_path: str | Path,
    media_cache_dir: str | os.PathLike[str],
    fallback_lang_code: str = "E",
) -> None:
    """Write a JW Library playlist and normalize infrastructure failures."""
    try:
        _write_jwlplaylist(
            playlist_name,
            items,
            output_path,
            media_cache_dir,
            fallback_lang_code=fallback_lang_code,
        )
    except (OSError, sqlite3.Error, zipfile.LargeZipFile, struct.error) as exc:
        raise PlaylistWriteError(f"Could not write playlist to {output_path}") from exc


# ── Thumbnail padrão (1×1 pixel PNG cinza) ────────────────────────────────────
#
# Incluído no ZIP como "default_thumbnail.png" — idêntico ao comportamento
# real do JW Library para playlists sem thumbnail global.
_DEFAULT_THUMBNAIL_PNG: bytes = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _build_manifest(playlist_name: str, db_bytes: bytes) -> str:
    """
    Gera o manifest.json idêntico ao gerado pelo JW Library (versão ≥ 14).

    Formato real (verificado contra pacote gerado pelo JW Library):
    {
        "name": "<nome>.jwlplaylist",
        "creationDate": "2026-03-11T17:09:31.3479402-03:00",
        "version": 1,
        "type": 1,
        "userDataBackup": {
            "lastModifiedDate": "2026-03-11T20:09:31+00:00",
            "deviceName": "PC",
            "databaseName": "userData.db",
            "hash": "<sha256 do userData.db>",
            "schemaVersion": 14
        }
    }

    Notas importantes:
      - type = 1 (não 14; 14 é a versão do schema do DB, não o tipo do pacote)
      - creationDate = datetime local com offset de fuso (ex: -03:00)
      - lastModifiedDate = datetime UTC com sufixo +00:00 (não Z)
      - databaseName = "userData.db" (campo obrigatório)
      - schemaVersion = 14 (nome correto; "databaseVersion" causa falha de importação)
      - name inclui a extensão ".jwlplaylist"
      - hash = SHA-256 do userData.db serializado
    """
    import datetime
    import json

    now_utc   = datetime.datetime.now(datetime.timezone.utc)
    now_local = now_utc.astimezone()   # converte para o fuso local da máquina

    # JW Library usa formato com microssegundos e offset local, ex:
    # "2026-03-11T17:09:31.3479402-03:00"
    # Python strftime não suporta 7 dígitos fracionários; usamos 6 (microsegundos)
    # e acrescentamos o offset de fuso manualmente.
    local_offset = now_local.strftime("%z")            # ex: "-0300"
    if local_offset:
        # formata como "-03:00"
        local_offset = local_offset[:3] + ":" + local_offset[3:]
    else:
        local_offset = "+00:00"

    creation_date = now_local.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0" + local_offset

    # lastModifiedDate em UTC com "+00:00" (não "Z")
    last_modified = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # O nome inclui a extensão .jwlplaylist (comportamento real do JW Library)
    name_with_ext = (
        playlist_name if playlist_name.lower().endswith(".jwlplaylist")
        else playlist_name + ".jwlplaylist"
    )

    db_hash = _sha256(db_bytes)

    manifest = {
        "name":         name_with_ext,
        "creationDate": creation_date,
        "version":      1,
        "type":         1,
        "userDataBackup": {
            "lastModifiedDate": last_modified,
            "deviceName":       "PC",
            "databaseName":     "userData.db",
            "hash":             db_hash,
            "schemaVersion":    14,
        },
    }
    return json.dumps(manifest, ensure_ascii=False, indent=4)


def _create_schema(con: sqlite3.Connection) -> None:
    """Cria o schema SQLite compatível com JW Library (baseado no userData.db real).

    PRAGMA user_version = 14 é obrigatório: o JW Library verifica este valor
    ao importar o pacote para confirmar compatibilidade de schema (= schemaVersion
    no manifest.json). Sem ele, a importação falha silenciosamente.
    """
    create_jwlplaylist_schema(con)


def _serialize_db(con: sqlite3.Connection) -> bytes:
    """Serializa conexão SQLite para bytes."""
    if hasattr(con, "serialize"):
        return con.serialize()
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="solin_export_")
    dst: sqlite3.Connection | None = None
    try:
        os.close(fd)
        dst = sqlite3.connect(tmp)
        con.backup(dst)
        dst.close()
        dst = None
        return Path(tmp).read_bytes()
    finally:
        if dst is not None:
            try:
                dst.close()
            except sqlite3.Error:
                log.debug("Failed to close temporary export database", exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            log.debug("Failed to remove temporary export database %s", tmp, exc_info=True)
