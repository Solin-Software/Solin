"""
reader.py
─────────────────────
Lê arquivos .jwlplaylist exportados pelo JW Library ≥ 14.

Formato:
  • ZIP contendo userData.db (SQLite) + imagens embutidas
  • Tabelas relevantes:
      - Playlist              → metadados da playlist (nome, etc.)
      - PlaylistItem          → itens ordenados (Label, position)
      - IndependentMedia      → mídias locais embutidas no ZIP
                                (FilePath, OriginalFileName, MimeType, Hash)
      - PlaylistItemIndependentMediaMap → liga PlaylistItem ↔ IndependentMedia
      - PlaylistItemLocationMap         → liga PlaylistItem ↔ Location (vídeos JW.org)
      - Location              → referência de conteúdo JW.org
                                (KeySymbol, Track, IssueTagNumber, DocumentId, …)

Resultado de `parse()`:
  {
    "name": str,                  # nome da playlist
    "items": [
        {
            "title":      str,
            "type":       "image" | "video",
            "source":     "embedded" | "jworg",
            # — para imagens embutidas:
            "data":       bytes,          # conteúdo da imagem
            "mime_type":  str,
            "filename":   str,
            # — para vídeos JW.org:
            "jworg_url":  str | None,     # URL resolvida (pode ser None se offline)
            "key_symbol": str,
            "track":      int | None,
            "issue_tag":  int | None,
            "doc_id":     int | None,
            "language":   int,
        },
        …
    ]
  }

Resolução de URLs JW.org:
  Tenta https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS?...
  Se a rede não estiver disponível, retorna jworg_url=None — o chamador
  pode exibir uma mensagem ao usuário ou pular o item.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.network.http import urlopen as _urlopen
from app.core.foundation.constants import (
    VIDEO_PREFERRED_QUALITY,
    VIDEO_QUALITY_FALLBACK_DIR,
    VIDEO_QUALITY_ORDER,
)
from app.core.jw.metadata import _LANG_FROM_MEPS

log = logging.getLogger(__name__)

# ── Constantes JW.org ──────────────────────────────────────────────────────────
_JWORG_API = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
_TIMEOUT    = 8   # segundos

# Mapa simples MimeType → extensão de arquivo
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg":   ".jpg",
    "image/jpg":    ".jpg",
    "image/png":    ".png",
    "image/gif":    ".gif",
    "image/webp":   ".webp",
    "image/bmp":    ".bmp",
    "image/svg+xml":".svg",
    "video/mp4":    ".mp4",
    "video/webm":   ".webm",
    "audio/mpeg":   ".mp3",
    "audio/mp4":    ".m4a",
    "audio/ogg":    ".ogg",
}


# ── Dataclasses internos ──────────────────────────────────────────────────────

@dataclass
class _RawItem:
    """Representa um item bruto da tabela PlaylistItem."""
    playlist_item_id:      int
    label:                 str
    position:              int
    start_trim_ticks:      Optional[int] = None
    end_trim_ticks:        Optional[int] = None
    accuracy:              Optional[int] = None
    end_action:            Optional[int] = None
    thumbnail_file_path:   Optional[str] = None
    # preenchidos depois da junção com as tabelas de mapa:
    independent_media_id: Optional[int] = None
    location_id:          Optional[int] = None


@dataclass
class _IndependentMedia:
    filepath:      str          # relativo dentro do ZIP
    original_name: str
    mime_type:     str
    hash_:         str = ""
    data:          bytes = field(default_factory=bytes, repr=False)


@dataclass
class _Location:
    location_id:           int
    key_symbol:            str
    track:                 Optional[int]
    issue_tag:             Optional[int]
    doc_id:                Optional[int]
    language_id:           int = 0
    meps_language:         int = 0
    major_multimedia_type: Optional[int] = None  # 0=audio, 2=video (da PlaylistItemLocationMap)


# ── Parser principal ──────────────────────────────────────────────────────────

class PlaylistReadError(ValueError):
    """A playlist archive exists but its internal data cannot be read."""


class JWLPlaylistReader:
    """Lê um .jwlplaylist e retorna a estrutura de playlist normalizada."""

    def __init__(self, path: str | Path, fallback_lang_code: str = "E"):
        self._path = Path(path)
        self._fallback_lang_code = fallback_lang_code

    def parse(self) -> dict:
        """
        Retorna dict com 'name' e 'items'.
        Lança FileNotFoundError, zipfile.BadZipFile ou ValueError em caso de erro.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {self._path}")

        try:
            with zipfile.ZipFile(self._path, "r") as zf:
                self._zip = zf
                self._names_in_zip = set(zf.namelist())
                return self._parse_zip()
        except zipfile.BadZipFile:
            raise
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            RuntimeError,
            zipfile.LargeZipFile,
        ) as exc:
            raise PlaylistReadError("Invalid or unreadable playlist archive") from exc

    # ── Internos ──────────────────────────────────────────────────────────────

    def _parse_zip(self) -> dict:
        # Localiza userData.db (pode estar na raiz ou numa subpasta)
        db_entry = self._find_db()
        if not db_entry:
            raise ValueError("userData.db não encontrado no arquivo .jwlplaylist")

        db_bytes = self._zip.read(db_entry)
        return self._parse_db(db_bytes)

    def _find_db(self) -> Optional[str]:
        for name in self._names_in_zip:
            if name.endswith("userData.db"):
                return name
        return None

    def _parse_db(self, db_bytes: bytes) -> dict:
        """
        Carrega os bytes do SQLite via arquivo temporário → backup para :memory:.

        Essa abordagem é a mais robusta e funciona em 100% dos casos:
          - Não depende de deserialize() (disponível só no Python 3.11+ e com
            edge cases em tamanhos de página não-padrão).
          - Não mantém arquivos temporários abertos durante a extração
            (evita WinError 32 no Windows).
        """
        import tempfile as _tmp

        fd, tmp_path = _tmp.mkstemp(suffix=".db", prefix="solin_jwl_")
        src: sqlite3.Connection | None = None
        mem: sqlite3.Connection | None = None
        try:
            os.write(fd, db_bytes)
            os.close(fd)
            fd = -1

            src = sqlite3.connect(tmp_path)
            mem = sqlite3.connect(":memory:")
            src.backup(mem)
        except (OSError, sqlite3.Error) as exc:
            log.error("Falha ao carregar userData.db: %s", exc)
            try:
                if fd >= 0:
                    os.close(fd)
            except OSError:
                log.debug("Could not close temporary playlist descriptor", exc_info=True)
            if mem is not None:
                try:
                    mem.close()
                except sqlite3.Error:
                    log.debug("Could not close in-memory playlist database", exc_info=True)
            raise PlaylistReadError("Invalid or unreadable userData.db") from exc
        finally:
            if src is not None:
                try:
                    src.close()
                except sqlite3.Error:
                    log.debug("Could not close source playlist database", exc_info=True)
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove temporary playlist database %s", tmp_path, exc_info=True)

        if mem is None:
            raise PlaylistReadError("Could not initialize playlist database")
        mem.row_factory = sqlite3.Row
        try:
            return self._extract(mem)
        except sqlite3.Error as exc:
            log.error("Falha ao consultar userData.db: %s", exc)
            raise PlaylistReadError("Invalid playlist database schema") from exc
        finally:
            try:
                mem.close()
            except sqlite3.Error:
                log.debug("Could not close in-memory playlist database", exc_info=True)

    def _extract(self, con: sqlite3.Connection) -> dict:
        # ── Nome da playlist ──────────────────────────────────────────────────
        playlist_name = self._get_playlist_name(con)

        # ── Itens ordenados ───────────────────────────────────────────────────
        raw_items = self._get_raw_items(con)

        # ── Mapa item → mídia embutida ────────────────────────────────────────
        im_map = self._build_independent_media_map(con)

        # ── Mapa item → location (vídeo JW.org) ──────────────────────────────
        loc_map = self._build_location_map(con)

        # ── Constrói lista de resultado ───────────────────────────────────────
        items = []
        for raw in raw_items:
            iid = raw.playlist_item_id

            if iid in im_map:
                media = im_map[iid]
                # Roteia pelo mime_type — IndependentMedia pode conter vídeo também
                mime = (media.mime_type or "").lower()
                if mime.startswith("video/") or mime.startswith("audio/"):
                    entry = self._build_embedded_media_entry(raw, media)
                else:
                    entry = self._build_image_entry(raw, media)
            elif iid in loc_map:
                loc = loc_map[iid]
                entry = self._build_video_entry(raw, loc)
            else:
                log.warning("PlaylistItem %d sem midia associada — ignorado.", iid)
                continue

            items.append(entry)

        return {"name": playlist_name, "items": items}

    # ── Helpers de extração ───────────────────────────────────────────────────

    def _get_playlist_name(self, con: sqlite3.Connection) -> str:
        # O schema real do JW Library não possui tabela "Playlist".
        # Usa o nome do arquivo (sem extensão) como nome da playlist.
        return self._path.stem or "Playlist"

    def _get_raw_items(self, con: sqlite3.Connection) -> list[_RawItem]:
        """
        Lê os itens da PlaylistItem de forma robusta.

        O JW Library mudou o schema entre versões:
          - v14+  usa a coluna  "Position"  (inteiro, base-0)
          - Versões mais antigas usam "Order"
          - Pode haver outras variações (ex.: sem coluna de ordem alguma)

        Estratégia:
          1. Inspeciona as colunas reais via PRAGMA table_info.
          2. Monta a query com as colunas existentes.
          3. Se nenhuma coluna de ordem for encontrada, ordena por rowid
             (preserva a ordem de inserção original do JW Library).
        """
        # Colunas reais da tabela (em minúsculas para comparação case-insensitive)
        pragma_rows = con.execute("PRAGMA table_info(PlaylistItem)").fetchall()
        col_names_lower = {row[1].lower(): row[1] for row in pragma_rows}
        # row[1] é o nome original da coluna; usamos o mapa lower→original

        # ── Coluna de ID ───────────────────────────────────────────────────────
        id_col = (
            col_names_lower.get("playlistitemid")
            or col_names_lower.get("id")
            or "rowid"
        )

        # ── Coluna de rótulo ───────────────────────────────────────────────────
        label_col = (
            col_names_lower.get("label")
            or col_names_lower.get("title")
            or col_names_lower.get("name")
        )

        # ── Coluna de ordem ───────────────────────────────────────────────────
        # Possíveis nomes já observados nos diferentes builds do JW Library:
        #   "Position", "Order", "Sequence", "Sort", "SortOrder"
        order_col = (
            col_names_lower.get("position")
            or col_names_lower.get("order")
            or col_names_lower.get("sequence")
            or col_names_lower.get("sort")
            or col_names_lower.get("sortorder")
        )

        log.debug(
            "PlaylistItem colunas detectadas -> id=%s  label=%s  order=%s",
            id_col, label_col, order_col,
        )

        def _q(col: str) -> str:
            """Envolve o nome da coluna em aspas duplas (escapa palavras reservadas como Order)."""
            return '"' + col.replace('"', '""') + '"'

        # Monta SELECT dinamico — sempre lê todas as colunas conhecidas que existirem
        def _col(name: str) -> Optional[str]:
            return col_names_lower.get(name.lower())

        select_parts = [_q(id_col)]
        if label_col:
            select_parts.append(_q(label_col))
        if order_col:
            select_parts.append(_q(order_col))
            order_clause = "ORDER BY " + _q(order_col)
        else:
            order_clause = "ORDER BY rowid"

        # Colunas extras (opcionais)
        extra_cols = {
            "start_trim":  _col("StartTrimOffsetTicks"),
            "end_trim":    _col("EndTrimOffsetTicks"),
            "accuracy":    _col("Accuracy"),
            "end_action":  _col("EndAction"),
            "thumbnail":   _col("ThumbnailFilePath"),
        }
        for alias, orig in extra_cols.items():
            if orig:
                select_parts.append(f'{_q(orig)} AS {alias}')

        sql = "SELECT " + ", ".join(select_parts) + " FROM PlaylistItem " + order_clause
        log.debug("Query PlaylistItem: %s", sql)

        rows = con.execute(sql).fetchall()

        result: list[_RawItem] = []
        for idx, r in enumerate(rows):
            try:
                pid = r[id_col]
            except (IndexError, KeyError):
                pid = r[0]

            if label_col:
                try:
                    lbl = r[label_col] or ""
                except (IndexError, KeyError):
                    lbl = ""
            else:
                lbl = ""

            if order_col:
                try:
                    pos = int(r[order_col])
                except (IndexError, KeyError, TypeError, ValueError):
                    pos = idx
            else:
                pos = idx

            def _safe(alias: str, row=r):
                try:
                    return row[alias]
                except (IndexError, KeyError):
                    return None

            result.append(_RawItem(
                playlist_item_id    = pid,
                label               = lbl or f"Item {pos + 1}",
                position            = pos,
                start_trim_ticks    = _safe("start_trim"),
                end_trim_ticks      = _safe("end_trim"),
                accuracy            = _safe("accuracy"),
                end_action          = _safe("end_action"),
                thumbnail_file_path = _safe("thumbnail"),
            ))

        return result

    def _build_independent_media_map(self, con: sqlite3.Connection) -> dict[int, _IndependentMedia]:
        """
        Retorna {PlaylistItemId: _IndependentMedia} para itens com mídia embutida.
        Carrega os bytes da imagem direto do ZIP.
        """
        result: dict[int, _IndependentMedia] = {}
        try:
            rows = con.execute(
                """
                SELECT
                    m.PlaylistItemId,
                    im.FilePath,
                    im.OriginalFilename,
                    im.MimeType,
                    COALESCE(im.Hash, '') AS Hash
                FROM PlaylistItemIndependentMediaMap m
                JOIN IndependentMedia im
                  ON m.IndependentMediaId = im.IndependentMediaId
                """
            ).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("Tabela de mídia independente não encontrada: %s", e)
            return result

        for row in rows:
            file_path  = row["FilePath"]
            mime_type  = row["MimeType"] or "image/jpeg"
            orig_name  = row["OriginalFilename"] or file_path

            # Tenta ler os bytes do ZIP (busca exata e parcial)
            data = self._read_zip_entry(file_path)
            if data is None:
                log.warning("Arquivo %s não encontrado no ZIP — item ignorado.", file_path)
                continue

            result[row["PlaylistItemId"]] = _IndependentMedia(
                filepath      = file_path,
                original_name = orig_name,
                mime_type     = mime_type,
                hash_         = row["Hash"],
                data          = data,
            )

        return result

    def _build_location_map(self, con: sqlite3.Connection) -> dict[int, _Location]:
        """
        Retorna {PlaylistItemId: _Location} para itens que referenciam vídeos JW.org.
        """
        result: dict[int, _Location] = {}
        try:
            rows = con.execute(
                """
                SELECT
                    m.PlaylistItemId,
                    l.LocationId,
                    COALESCE(l.KeySymbol, '')         AS KeySymbol,
                    l.Track,
                    l.IssueTagNumber,
                    l.DocumentId,
                    COALESCE(l.MepsLanguage, 0)       AS MepsLanguage,
                    m.MajorMultimediaType,
                    m.BaseDurationTicks
                FROM PlaylistItemLocationMap m
                JOIN Location l ON m.LocationId = l.LocationId
                """
            ).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("Tabela Location não encontrada: %s", e)
            return result

        for row in rows:
            result[row["PlaylistItemId"]] = _Location(
                location_id           = row["LocationId"],
                key_symbol            = row["KeySymbol"],
                track                 = row["Track"],
                issue_tag             = row["IssueTagNumber"],
                doc_id                = row["DocumentId"],
                meps_language         = row["MepsLanguage"],
                major_multimedia_type = row["MajorMultimediaType"],
            )

        return result

    def _read_zip_entry(self, file_path: str) -> Optional[bytes]:
        """Tenta ler uma entrada do ZIP por caminho exato ou correspondência parcial."""
        if file_path in self._names_in_zip:
            return self._zip.read(file_path)
        # Às vezes o FilePath no banco tem separadores diferentes ou subpasta
        basename = Path(file_path).name
        for name in self._names_in_zip:
            if Path(name).name == basename:
                return self._zip.read(name)
        return None

    # ── Construtores de entry ─────────────────────────────────────────────────

    def _build_embedded_media_entry(self, raw: _RawItem, media: _IndependentMedia) -> dict:
        """Constrói entry para vídeo/áudio embutido no ZIP (IndependentMedia)."""
        mime = (media.mime_type or "").lower()
        if mime.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "video"

        return {
            "title":             raw.label,
            "type":              media_type,
            "source":            "embedded",
            "data":              media.data,
            "mime_type":         media.mime_type,
            "filename":          media.original_name,
            "url":               None,
            "start_trim_ticks":  raw.start_trim_ticks,
            "end_trim_ticks":    raw.end_trim_ticks,
            "accuracy":          raw.accuracy,
            "end_action":        raw.end_action,
        }

    def _build_image_entry(self, raw: _RawItem, media: _IndependentMedia) -> dict:
        ext = _MIME_TO_EXT.get(media.mime_type.lower(), "")
        if not ext:
            ext = Path(media.original_name).suffix or ".jpg"

        return {
            "title":             raw.label,
            "type":              "image",
            "source":            "embedded",
            "data":              media.data,
            "mime_type":         media.mime_type,
            "filename":          media.original_name,
            "url":               None,
            "start_trim_ticks":  raw.start_trim_ticks,
            "end_trim_ticks":    raw.end_trim_ticks,
            "accuracy":          raw.accuracy,
            "end_action":        raw.end_action,
        }

    def _build_video_entry(self, raw: _RawItem, loc: _Location) -> dict:
        meta = resolve_jworg_metadata(
            key_symbol            = loc.key_symbol,
            track                 = loc.track,
            issue_tag             = loc.issue_tag,
            doc_id                = loc.doc_id,
            meps_language         = loc.meps_language,
            fallback_lang_code    = self._fallback_lang_code,
            major_multimedia_type = loc.major_multimedia_type,
        )
        url            = meta["url"]            if meta else None
        duration_ticks = meta["duration_ticks"] if meta else None
        # Título canônico da API (ex: "Faça amizade com os mais velhos") tem
        # prioridade sobre o Label do banco — que pode ter sido editado/sufixado.
        api_title      = meta.get("title")      if meta else None

        if loc.major_multimedia_type == 0:
            media_type = "audio"
        elif loc.major_multimedia_type == 2:
            media_type = "video"
        else:
            _AUDIO_PREFIXES = ("sjj", "osg", "ia", "km")
            media_type = "audio" if (loc.key_symbol or "").lower().startswith(_AUDIO_PREFIXES) else "video"

        return {
            "title":                  api_title or raw.label,
            "type":                   media_type,
            "source":                 "jworg",
            "data":                   None,
            "jworg_url":              url,
            "url":                    url,
            "key_symbol":             loc.key_symbol,
            "track":                  loc.track,
            "issue_tag":              loc.issue_tag,
            "doc_id":                 loc.doc_id,
            "language":               loc.meps_language,
            "meps_language":          loc.meps_language,
            "major_multimedia_type":  loc.major_multimedia_type,
            "base_duration_ticks":    duration_ticks,
            "start_trim_ticks":       raw.start_trim_ticks,
            "end_trim_ticks":         raw.end_trim_ticks,
            "accuracy":               raw.accuracy,
            "end_action":             raw.end_action,
        }
        
    def print_schema(self) -> None:
        """
        Lê o userData.db do ZIP e imprime na tela o esquema real de todas as tabelas.
        Isso ignora a extração normal e serve apenas para debug.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {self._path}")

        with zipfile.ZipFile(self._path, "r") as zf:
            self._zip = zf
            self._names_in_zip = set(zf.namelist())
            
            db_entry = self._find_db()
            if not db_entry:
                log.error("userData.db não encontrado no arquivo .jwlplaylist")
                return
                
            db_bytes = self._zip.read(db_entry)
            
            # Reutiliza o sistema robusto de conexão do _parse_db, mas altera o extrator final
            # Em vez de retornar self._extract, passamos uma função de dump temporária
            self._dump_schema(db_bytes)
            
    def _dump_schema(self, db_bytes: bytes):
        import tempfile
        import os
        
        fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="debug_jwl_")
        os.write(fd, db_bytes)
        os.close(fd)

        con = None
        try:
            con = sqlite3.connect(tmp_path)
            con.row_factory = sqlite3.Row

            lines = [
                "",
                "=" * 50,
                f" ESQUEMA DO BANCO: {self._path.name}",
                "=" * 50,
            ]
            
            # Pega todas as tabelas
            tables = con.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            
            for table_row in tables:
                table_name = table_row["name"]
                lines.append("")
                lines.append(f"Tabela: {table_name}")
                
                # Pega as colunas de cada tabela
                columns = con.execute(f"PRAGMA table_info('{table_name}')").fetchall()
                for col in columns:
                    pk_marker = " (PRIMARY KEY)" if col["pk"] else ""
                    lines.append(f"  - {col['name']} -> {col['type']}{pk_marker}")

            lines.extend(["", "=" * 50, ""])
            log.info("\n%s", "\n".join(lines))
                    
        finally:
            if con is not None:
                con.close()
            Path(tmp_path).unlink(missing_ok=True)


# ── Função solta no final do arquivo (perto do read_jwlplaylist) ──────────────

def introspect_jwlplaylist(path: str | Path) -> None:
    """
    Abre um .jwlplaylist e imprime a estrutura real do banco de dados na tela.
    Uso: introspect_jwlplaylist('minha_lista.jwlplaylist')
    """
    reader = JWLPlaylistReader(path)
    reader.print_schema()

# introspect_jwlplaylist("C:/Users/TestUser/Downloads/teste.jwlplaylist")


# ── Resolução de URL JW.org ───────────────────────────────────────────────────

def resolve_jworg_url(
    key_symbol:             str,
    track:                  Optional[int] = None,
    issue_tag:              Optional[int] = None,
    doc_id:                 Optional[int] = None,
    meps_language:          int           = 0,
    quality:                str           = VIDEO_PREFERRED_QUALITY,
    fallback_lang_code:     str           = "E",
    major_multimedia_type:  Optional[int] = None,
) -> Optional[str]:
    """
    Consulta a API publica do JW.org para obter a URL de streaming/download.

    BUG 3 FIX: major_multimedia_type=0 → áudio → fileformat=MP3.
    major_multimedia_type=2 (ou None/outro) → vídeo → fileformat=MP4.

    A API retorna seções diferentes dependendo do fileformat pedido:
      files > <lang_code> > MP4 > [...]   para vídeos
      files > <lang_code> > MP3 > [...]   para áudios

    fallback_lang_code: api_code do LanguageManager (usado quando meps_language
    nao esta no mapa conhecido).
    """
    # Precisa de pelo menos key_symbol OU doc_id para identificar a publicação
    if not key_symbol and not doc_id:
        return None

    # Decide o formato com base no tipo da mídia
    is_audio  = (major_multimedia_type == 0)
    fileformat = "MP3" if is_audio else "MP4"

    lang_code = _meps_to_lang_code(meps_language, fallback=fallback_lang_code)

    params: dict[str, str] = {
        "langwritten": lang_code,
        "fileformat":  fileformat,
    }
    if key_symbol:
        params["pub"] = key_symbol
    if track is not None:
        params["track"] = str(track)
    if issue_tag:
        params["issue"] = str(issue_tag)
    if doc_id:
        params["docid"] = str(doc_id)

    api_url = f"{_JWORG_API}?{urllib.parse.urlencode(params)}"
    log.debug("Resolvendo URL JW.org (%s): %s", fileformat, api_url)

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Solin/1.0"})
        with _urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning("Nao foi possivel resolver URL JW.org para '%s': %s", key_symbol, e)
        return None

    result = _extract_best_entry(data, lang_code=lang_code, preferred_quality=quality,
                                 fileformat=fileformat)
    if result is None:
        return None
    return result["url"]


def resolve_jworg_metadata(
    key_symbol:             str,
    track:                  Optional[int] = None,
    issue_tag:              Optional[int] = None,
    doc_id:                 Optional[int] = None,
    meps_language:          int           = 0,
    quality:                str           = VIDEO_PREFERRED_QUALITY,
    fallback_lang_code:     str           = "E",
    major_multimedia_type:  Optional[int] = None,
) -> Optional[dict]:
    """
    Como resolve_jworg_url, mas retorna um dict completo:
      { "url": str, "duration_ticks": int | None }

    duration_ticks = duration_seconds × 10_000_000
    (1 tick = 100 ns, padrão Windows FILETIME — idêntico ao JW Library).

    Retorna None se não conseguir resolver.
    """
    # Precisa de pelo menos key_symbol OU doc_id para identificar a publicação
    if not key_symbol and not doc_id:
        return None

    is_audio   = (major_multimedia_type == 0)
    fileformat = "MP3" if is_audio else "MP4"
    lang_code  = _meps_to_lang_code(meps_language, fallback=fallback_lang_code)

    params: dict[str, str] = {
        "langwritten": lang_code,
        "fileformat":  fileformat,
    }
    if key_symbol:
        params["pub"] = key_symbol
    if track is not None:
        params["track"] = str(track)
    if issue_tag:
        params["issue"] = str(issue_tag)
    if doc_id:
        params["docid"] = str(doc_id)

    api_url = f"{_JWORG_API}?{urllib.parse.urlencode(params)}"
    log.debug("Resolvendo metadados JW.org (%s): %s", fileformat, api_url)

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Solin/1.0"})
        with _urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning("Não foi possível resolver metadados JW.org para '%s': %s", key_symbol, e)
        return None

    entry = _extract_best_entry(data, lang_code=lang_code, preferred_quality=quality,
                                fileformat=fileformat)
    if entry is None:
        return None

    # duration na API é em segundos (float); converte para ticks de 100 ns
    duration_s     = entry.get("duration_seconds")
    duration_ticks = int(duration_s * 10_000_000) if duration_s else None

    return {
        "url":            entry["url"],
        "duration_ticks": duration_ticks,
        "title":          entry.get("title"),   # título canônico da API (pode ser None)
    }


def _extract_best_entry(
    api_response:      dict,
    lang_code:         str = "T",
    preferred_quality: str = VIDEO_PREFERRED_QUALITY,
    fileformat:        str = "MP4",
) -> Optional[dict]:
    """
    Navega pela resposta da API JW.org e retorna um dict com:
      { "url": str, "duration_seconds": float | None }

    Para MP3: maior bitrate disponível.
    Para MP4: usa VIDEO_PREFERRED_QUALITY + VIDEO_QUALITY_FALLBACK_DIR (constants.py),
    sem legenda. Degrada conforme a direção configurada se necessário.
    """
    files = api_response.get("files", {})
    fmt   = fileformat.upper()

    lang_section = files.get(lang_code, {})
    entries: list[dict] = lang_section.get(fmt, [])

    if not entries:
        for section in files.values():
            if isinstance(section, dict):
                candidate = section.get(fmt, [])
                if candidate:
                    entries = candidate
                    break

    if not entries:
        log.warning("Nenhum arquivo %s encontrado na resposta da API (lang=%s).", fmt, lang_code)
        return None

    def _url_from_entry(e: dict) -> Optional[str]:
        return (
            (e.get("file") or {}).get("url")
            or e.get("progressiveDownloadURL")
            or e.get("url")
        )

    def _make_result(e: dict) -> Optional[dict]:
        url = _url_from_entry(e)
        if not url:
            return None
        # A API devolve "duration" em segundos (float) em cada entrada de arquivo
        dur = e.get("duration") or (e.get("file") or {}).get("duration")
        # "title" na entrada da API é o título oficial da faixa
        title = e.get("title")
        # Labels de qualidade ("720p", "480p"…) não são títulos
        if title and re.match(r'^\d+[pP]$|^\d+kbps$', title.strip(), re.I):
            title = None
        return {
            "url":              url,
            "duration_seconds": float(dur) if dur else None,
            "title":            title.strip() if title else None,
        }

    # ── Áudio (MP3) ───────────────────────────────────────────────────────────
    if fmt == "MP3":
        for bitrate in ["320kbps", "256kbps", "192kbps", "128kbps", "96kbps", "64kbps", "32kbps"]:
            for e in entries:
                if isinstance(e, dict) and e.get("label") == bitrate:
                    r = _make_result(e)
                    if r:
                        log.debug("MP3 selecionado: label=%s  %s", bitrate, r["url"])
                        return r
        for e in entries:
            if isinstance(e, dict):
                r = _make_result(e)
                if r:
                    return r
        return None

    # ── Vídeo (MP4) — qualidade centralizada de constants.py ──────────────────
    labels_found = {e.get("label") for e in entries if isinstance(e, dict)}

    # Constrói fallback seguindo VIDEO_QUALITY_FALLBACK_DIR
    order = list(VIDEO_QUALITY_ORDER)
    if preferred_quality in order:
        idx   = order.index(preferred_quality)
        above = [q for q in order[:idx]   if q in labels_found]
        below = [q for q in order[idx+1:] if q in labels_found]
    else:
        above = [q for q in order if q in labels_found]
        below = []

    if VIDEO_QUALITY_FALLBACK_DIR == "above":
        quality_order = [preferred_quality] + above + below
    else:  # "below" (padrão)
        quality_order = [preferred_quality] + below + above

    # Labels desconhecidos ao final (robustez a novas resoluções JW)
    quality_order += [q for q in labels_found if q not in quality_order]

    for q in quality_order:
        for e in entries:
            if not isinstance(e, dict): continue
            if e.get("label") == q and e.get("subtitled") is False:
                r = _make_result(e)
                if r:
                    log.debug("MP4 selecionado: label=%s subtitled=False  %s", q, r["url"])
                    return r

    for q in quality_order:
        for e in entries:
            if not isinstance(e, dict): continue
            if e.get("label") == q:
                r = _make_result(e)
                if r:
                    log.debug("MP4 selecionado (com legenda): label=%s  %s", q, r["url"])
                    return r

    log.warning("Nenhuma URL utilizável encontrada nas entradas da API.")
    return None

def _meps_to_lang_code(meps_language: int, fallback: str = "E") -> str:
    """
    Converte o ID de idioma MEPS (usado internamente pelo JW Library) para o
    codigo de lingua usado na API publica do JW.org (ex: "T" = portugues).

    Apenas os IDs sao mapeados aqui.  Para qualquer ID desconhecido
    o fallback é usado -- idealmente o api_code do LanguageManager do app, de
    forma que o idioma da interface sirva como lingua padrao para os videos.

    IDs:
      0    -> "E"  English
      651  -> "S"  Espanol
      5    -> "T"  Portugues
    """
    code = _LANG_FROM_MEPS.get(meps_language)
    if code is None:
        log.debug(
            "MEPS ID %d desconhecido — usando fallback de idioma do app: %s",
            meps_language, fallback,
        )
        return fallback
    return code

# ── Função de conveniência ────────────────────────────────────────────────────

def read_jwlplaylist(path: str | Path, fallback_lang_code: str = "E") -> dict:
    """
    Ponto de entrada simplificado.

    fallback_lang_code: api_code do LanguageManager (ex: "T" para pt_BR).
      Usado como idioma para videos cujo MEPS ID nao esta no mapa conhecido.

    Retorna dict com 'name' e 'items'.
    Propaga FileNotFoundError, BadZipFile ou ValueError.
    """
    return JWLPlaylistReader(path, fallback_lang_code=fallback_lang_code).parse()
