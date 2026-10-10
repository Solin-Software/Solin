"""
reader.py
=========
Read .jwlplaylist files exported by JW Library ≥ 14.

Format:
  • ZIP containing userData.db (SQLite) and embedded images.
  • Relevant tables:
      - PlaylistItem → ordered items (Label, position).
      - IndependentMedia → local media embedded in the ZIP
        (FilePath, OriginalFileName, MimeType, Hash).
      - PlaylistItemIndependentMediaMap → PlaylistItem ↔ IndependentMedia.
      - PlaylistItemLocationMap → PlaylistItem ↔ Location (JW.org videos).
      - Location → JW.org content reference
        (KeySymbol, Track, IssueTagNumber, DocumentId, …).

`parse()` result:
  {
    "name": str,  # playlist name
    "items": [
      {
        "source_item_id": str,  # stable PlaylistItem ID within this source
        "title": str,
        "type": "image" | "video",
        "source": "embedded" | "jworg",
        # For embedded images:
        "data": bytes,  # image contents
        "mime_type": str,
        "filename": str,
        # For JW.org videos:
        "jworg_url": str | None,  # resolved URL; may be None when offline
        "key_symbol": str,
        "track": int | None,
        "issue_tag": int | None,
        "doc_id": int | None,
        "language": int,
      },
      …
    ]
  }

JW.org URL resolution:
  Try https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS?...
  If the network is unavailable, return jworg_url=None; callers can
  display a message to the user or skip the item.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
import zipfile
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from solin.core.jw.metadata import ResolvedMediaMetadata, resolve_jworg_meta

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int | None], None]
CancelCallback = Callable[[], bool]

_READ_CHUNK_SIZE = 1024 * 1024

# Simple MimeType → file extension map
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
}


# ── Dataclasses internos ──────────────────────────────────────────────────────


@dataclass
class _RawItem:
    """Raw item from the PlaylistItem table."""

    playlist_item_id: int
    label: str
    position: int
    start_trim_ticks: Optional[int] = None
    end_trim_ticks: Optional[int] = None
    accuracy: Optional[int] = None
    end_action: Optional[int] = None
    thumbnail_file_path: Optional[str] = None
    # populated after joining the mapping tables:
    independent_media_id: Optional[int] = None
    location_id: Optional[int] = None


@dataclass
class _IndependentMedia:
    filepath: str  # relative path within the ZIP
    original_name: str
    mime_type: str
    duration_ticks: Optional[int] = None
    hash_: str = ""
    data: bytes = field(default_factory=bytes, repr=False)


@dataclass
class _Location:
    location_id: int
    key_symbol: str
    track: Optional[int]
    issue_tag: Optional[int]
    doc_id: Optional[int]
    language_id: int = 0
    meps_language: int = 0
    major_multimedia_type: Optional[int] = None  # 0=audio, 2=video (from PlaylistItemLocationMap)
    base_duration_ticks: Optional[int] = None


# ── Parser principal ──────────────────────────────────────────────────────────


class PlaylistReadError(ValueError):
    """A playlist archive exists but its internal data cannot be read."""


def _database_text(
    value: object,
    field_name: str,
    *,
    default: str | None = None,
    required: bool = False,
) -> str:
    """Validate a SQLite value that the playlist format defines as text."""
    if value is None:
        if required:
            raise PlaylistReadError(f"Missing required playlist field: {field_name}")
        return default or ""
    if not isinstance(value, str):
        raise PlaylistReadError(
            f"Invalid playlist field {field_name}: expected text, got {type(value).__name__}"
        )
    if not value:
        if required:
            raise PlaylistReadError(f"Empty required playlist field: {field_name}")
        if default is not None:
            return default
    return value


class JWLPlaylistReader:
    """Read a .jwlplaylist file and return the normalized playlist structure."""

    def __init__(
        self,
        path: str | Path,
        fallback_lang_code: str = "E",
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ):
        self._path = Path(path)
        self._fallback_lang_code = fallback_lang_code
        self._jw_meta_by_reference: dict[tuple[object, ...], ResolvedMediaMetadata] = {}
        self._progress_callback = progress_callback
        self._should_cancel = should_cancel

    def _raise_if_cancelled(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            raise CancelledError("JW Library playlist transfer cancelled")

    def _report_progress(
        self,
        phase: str,
        completed: int,
        total: int | None,
    ) -> None:
        if self._progress_callback is not None:
            self._progress_callback(phase, completed, total)

    def parse(self) -> dict:
        """
        Return a dict with 'name' and 'items'.
        Raise FileNotFoundError, zipfile.BadZipFile, or ValueError on failure.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {self._path}")

        self._raise_if_cancelled()
        self._report_progress("opening", 0, 1)
        try:
            with zipfile.ZipFile(self._path, "r") as zf:
                self._zip = zf
                self._names_in_zip = set(zf.namelist())
                self._report_progress("opening", 1, 1)
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
        # Find userData.db (may be at the root or in a subfolder).
        db_entry = self._find_db()
        if not db_entry:
            raise ValueError("userData.db não encontrado no arquivo .jwlplaylist")

        db_bytes = self._read_zip_entry(db_entry, phase="database")
        if db_bytes is None:
            raise ValueError("userData.db não encontrado no arquivo .jwlplaylist")
        return self._parse_db(db_bytes)

    def _find_db(self) -> Optional[str]:
        for name in self._names_in_zip:
            if name.endswith("userData.db"):
                return name
        return None

    def _parse_db(self, db_bytes: bytes) -> dict:
        """
        Load SQLite bytes through a temporary file, then back up to :memory:.

        This robust approach:
          - Does not depend on deserialize() (Python 3.11+ only, with edge cases
            for nonstandard page sizes).
          - Does not keep temporary files open during extraction
            (avoids WinError 32 on Windows).
        """
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="solin_jwl_")
        src: sqlite3.Connection | None = None
        mem: sqlite3.Connection | None = None
        try:
            with os.fdopen(fd, "wb") as temporary_database:
                fd = -1
                view = memoryview(db_bytes)
                for offset in range(0, len(view), _READ_CHUNK_SIZE):
                    self._raise_if_cancelled()
                    temporary_database.write(view[offset : offset + _READ_CHUNK_SIZE])

            src = sqlite3.connect(tmp_path)
            mem = sqlite3.connect(":memory:")
            src.backup(mem)
        except (OSError, sqlite3.Error) as exc:
            log.error("Failed to load userData.db: %s", exc)
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
                log.warning(
                    "Could not remove temporary playlist database %s", tmp_path, exc_info=True
                )

        if mem is None:
            raise PlaylistReadError("Could not initialize playlist database")
        mem.row_factory = sqlite3.Row
        try:
            return self._extract(mem)
        except PlaylistReadError:
            log.warning("Invalid playlist database contents", exc_info=True)
            raise
        except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            log.warning("Invalid playlist database contents", exc_info=True)
            raise PlaylistReadError("Invalid playlist database contents") from exc
        finally:
            try:
                mem.close()
            except sqlite3.Error:
                log.debug("Could not close in-memory playlist database", exc_info=True)

    def _extract(self, con: sqlite3.Connection) -> dict:
        # Playlist name
        playlist_name = self._get_playlist_name(con)

        # ── Itens ordenados ───────────────────────────────────────────────────
        raw_items = self._get_raw_items(con)
        self._raise_if_cancelled()

        # Item → embedded media map
        im_map = self._build_independent_media_map(con)

        # Item → location map (JW.org video)
        loc_map = self._build_location_map(con)

        # Build the result list.
        items = []
        total_items = len(raw_items)
        self._report_progress("items", 0, total_items)
        for index, raw in enumerate(raw_items):
            self._raise_if_cancelled()
            iid = raw.playlist_item_id

            if iid in im_map:
                media = im_map[iid]
                # Route by mime_type; IndependentMedia can also contain video.
                mime = (media.mime_type or "").lower()
                if mime.startswith("video/") or mime.startswith("audio/"):
                    entry = self._build_embedded_media_entry(raw, media)
                else:
                    entry = self._build_image_entry(raw, media)
            elif iid in loc_map:
                loc = loc_map[iid]
                entry = self._build_video_entry(raw, loc)
            else:
                log.warning("PlaylistItem %d has no associated media - ignored.", iid)
                self._report_progress("items", index + 1, total_items)
                continue

            entry["source_item_id"] = str(iid)
            items.append(entry)
            self._raise_if_cancelled()
            self._report_progress("items", index + 1, total_items)

        return {"name": playlist_name, "items": items}

    # Extraction helpers

    def _get_playlist_name(self, con: sqlite3.Connection) -> str:
        # The actual JW Library schema has no "Playlist" table.
        # Use the filename without its extension as the playlist name.
        return self._path.stem or "Playlist"

    def _get_raw_items(self, con: sqlite3.Connection) -> list[_RawItem]:
        """
        Read PlaylistItem entries reliably.

        JW Library schemas differ between versions:
          - v14+ uses "Position" (zero-based integer).
          - Older versions use "Order".
          - Other variations may have no ordering column.

        Strategy:
          1. Inspect actual columns using PRAGMA table_info.
          2. Build a query with existing columns.
          3. If no ordering column exists, sort by rowid to preserve
             JW Library's original insertion order.
        """
        # Actual table columns (lowercase for case-insensitive comparison)
        pragma_rows = con.execute("PRAGMA table_info(PlaylistItem)").fetchall()
        col_names_lower = {row[1].lower(): row[1] for row in pragma_rows}
        # row[1] is the original column name; use the lowercase→original map.

        # ID column
        id_col = col_names_lower.get("playlistitemid") or col_names_lower.get("id") or "rowid"

        # Label column
        label_col = (
            col_names_lower.get("label")
            or col_names_lower.get("title")
            or col_names_lower.get("name")
        )

        # Order column
        # Possible names observed in different JW Library builds:
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
            id_col,
            label_col,
            order_col,
        )

        def _q(col: str) -> str:
            """Double-quote the column name to escape reserved words such as Order."""
            return '"' + col.replace('"', '""') + '"'

        # Build SELECT dynamically, reading all known columns that exist.
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
            "start_trim": _col("StartTrimOffsetTicks"),
            "end_trim": _col("EndTrimOffsetTicks"),
            "accuracy": _col("Accuracy"),
            "end_action": _col("EndAction"),
            "thumbnail": _col("ThumbnailFilePath"),
        }
        for alias, orig in extra_cols.items():
            if orig:
                select_parts.append(f"{_q(orig)} AS {alias}")

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
                    lbl = _database_text(r[label_col], f"PlaylistItem.{label_col}")
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

            result.append(
                _RawItem(
                    playlist_item_id=pid,
                    label=lbl or f"Item {pos + 1}",
                    position=pos,
                    start_trim_ticks=_safe("start_trim"),
                    end_trim_ticks=_safe("end_trim"),
                    accuracy=_safe("accuracy"),
                    end_action=_safe("end_action"),
                    thumbnail_file_path=_safe("thumbnail"),
                )
            )

        return result

    def _build_independent_media_map(self, con: sqlite3.Connection) -> dict[int, _IndependentMedia]:
        """
        Return {PlaylistItemId: _IndependentMedia} for embedded media items.
        Load media bytes directly from the ZIP.
        """
        result: dict[int, _IndependentMedia] = {}
        map_columns = {
            str(row["name"]).lower()
            for row in con.execute("PRAGMA table_info(PlaylistItemIndependentMediaMap)").fetchall()
        }
        duration_column = "m.DurationTicks" if "durationticks" in map_columns else "NULL"
        try:
            rows = con.execute(
                f"""
                SELECT
                    m.PlaylistItemId,
                    {duration_column} AS DurationTicks,
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
            log.warning("Independent media table not found: %s", e)
            return result

        total_media = len(rows)
        embedded_bytes: dict[str, bytes | None] = {}
        self._report_progress("media_items", 0, total_media)
        for index, row in enumerate(rows):
            self._raise_if_cancelled()
            file_path = _database_text(
                row["FilePath"],
                "IndependentMedia.FilePath",
                required=True,
            )
            mime_type = _database_text(
                row["MimeType"],
                "IndependentMedia.MimeType",
                default="image/jpeg",
            )
            orig_name = _database_text(
                row["OriginalFilename"],
                "IndependentMedia.OriginalFilename",
                default=file_path,
            )
            hash_value = _database_text(
                row["Hash"],
                "IndependentMedia.Hash",
            )

            # Try reading ZIP bytes (exact and partial matches).
            if file_path not in embedded_bytes:
                embedded_bytes[file_path] = self._read_zip_entry(file_path)
            data = embedded_bytes[file_path]
            if data is None:
                log.warning("File %s not found in ZIP - item ignored.", file_path)
                self._report_progress("media_items", index + 1, total_media)
                continue

            result[row["PlaylistItemId"]] = _IndependentMedia(
                filepath=file_path,
                original_name=orig_name,
                mime_type=mime_type,
                duration_ticks=row["DurationTicks"],
                hash_=hash_value,
                data=data,
            )
            self._report_progress("media_items", index + 1, total_media)

        return result

    def _build_location_map(self, con: sqlite3.Connection) -> dict[int, _Location]:
        """Return {PlaylistItemId: _Location} for items referencing JW.org videos."""
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
            log.warning("Location table not found: %s", e)
            return result

        for row in rows:
            self._raise_if_cancelled()
            result[row["PlaylistItemId"]] = _Location(
                location_id=row["LocationId"],
                key_symbol=_database_text(
                    row["KeySymbol"],
                    "Location.KeySymbol",
                ),
                track=row["Track"],
                issue_tag=row["IssueTagNumber"],
                doc_id=row["DocumentId"],
                meps_language=row["MepsLanguage"],
                major_multimedia_type=row["MajorMultimediaType"],
                base_duration_ticks=row["BaseDurationTicks"],
            )

        return result

    def _read_zip_entry(
        self,
        file_path: str,
        *,
        phase: str = "media_bytes",
    ) -> Optional[bytes]:
        """Try reading a ZIP entry by exact path or partial match."""
        entry_name: str | None = None
        if file_path in self._names_in_zip:
            entry_name = file_path
        else:
            # FilePath in the database may use different separators or a subfolder.
            basename = Path(file_path).name
            for name in self._names_in_zip:
                if Path(name).name == basename:
                    entry_name = name
                    break
        if entry_name is None:
            return None

        total = self._zip.getinfo(entry_name).file_size
        completed = 0
        self._report_progress(phase, completed, total)
        destination = io.BytesIO()
        with self._zip.open(entry_name, "r") as source:
            while chunk := source.read(_READ_CHUNK_SIZE):
                self._raise_if_cancelled()
                destination.write(chunk)
                completed += len(chunk)
                self._report_progress(phase, completed, total)
        self._raise_if_cancelled()
        return destination.getvalue()

    # ── Construtores de entry ─────────────────────────────────────────────────

    def _build_embedded_media_entry(self, raw: _RawItem, media: _IndependentMedia) -> dict:
        """Build an entry for video/audio embedded in the ZIP (IndependentMedia)."""
        mime = (media.mime_type or "").lower()
        if mime.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "video"

        return {
            "title": raw.label,
            "type": media_type,
            "source": "embedded",
            "data": media.data,
            "mime_type": media.mime_type,
            "filename": media.original_name,
            "embedded_asset_key": media.filepath,
            "url": None,
            "start_trim_ticks": raw.start_trim_ticks,
            "end_trim_ticks": raw.end_trim_ticks,
            "base_duration_ticks": media.duration_ticks,
            "accuracy": raw.accuracy,
            "end_action": raw.end_action,
        }

    def _build_image_entry(self, raw: _RawItem, media: _IndependentMedia) -> dict:
        ext = _MIME_TO_EXT.get(media.mime_type.lower(), "")
        if not ext:
            ext = Path(media.original_name).suffix or ".jpg"

        return {
            "title": raw.label,
            "type": "image",
            "source": "embedded",
            "data": media.data,
            "mime_type": media.mime_type,
            "filename": media.original_name,
            "embedded_asset_key": media.filepath,
            "url": None,
            "start_trim_ticks": raw.start_trim_ticks,
            "end_trim_ticks": raw.end_trim_ticks,
            "base_duration_ticks": media.duration_ticks,
            "accuracy": raw.accuracy,
            "end_action": raw.end_action,
        }

    def _build_video_entry(self, raw: _RawItem, loc: _Location) -> dict:
        reference_key = (
            loc.key_symbol,
            loc.doc_id,
            loc.track,
            loc.issue_tag,
            loc.meps_language,
            self._fallback_lang_code,
            loc.major_multimedia_type,
        )
        meta = self._jw_meta_by_reference.get(reference_key)
        if meta is None:
            meta = resolve_jworg_meta(
                key_symbol=loc.key_symbol,
                doc_id=loc.doc_id,
                track=loc.track,
                issue_tag=loc.issue_tag,
                meps_language=loc.meps_language,
                fallback_lang=self._fallback_lang_code,
                major_multimedia_type=loc.major_multimedia_type,
            )
            if meta is not None:
                self._jw_meta_by_reference[reference_key] = meta
        url = meta["url"] if meta else None
        duration_ticks = (
            loc.base_duration_ticks
            if loc.base_duration_ticks is not None
            else (meta["duration_ticks"] if meta else None)
        )
        # The canonical API title (e.g. "Make Friends With Older Ones") takes
        # precedence over the database Label, which may have been edited or suffixed.
        api_title = meta.get("title") if meta else None

        if loc.major_multimedia_type == 0:
            media_type = "audio"
        elif loc.major_multimedia_type == 2:
            media_type = "video"
        else:
            _AUDIO_PREFIXES = ("sjj", "osg", "ia", "km")
            media_type = (
                "audio" if (loc.key_symbol or "").lower().startswith(_AUDIO_PREFIXES) else "video"
            )

        return {
            "title": api_title or raw.label,
            "type": media_type,
            "source": "jworg",
            "data": None,
            "jworg_url": url,
            "url": url,
            "key_symbol": loc.key_symbol,
            "track": loc.track,
            "issue_tag": loc.issue_tag,
            "doc_id": loc.doc_id,
            "language": loc.meps_language,
            "meps_language": loc.meps_language,
            "major_multimedia_type": loc.major_multimedia_type,
            "base_duration_ticks": duration_ticks,
            "start_trim_ticks": raw.start_trim_ticks,
            "end_trim_ticks": raw.end_trim_ticks,
            "accuracy": raw.accuracy,
            "end_action": raw.end_action,
        }

    def print_schema(self) -> None:
        """
        Read userData.db from the ZIP and print the actual schema of all tables.
        Bypass normal extraction; intended only for debugging.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {self._path}")

        with zipfile.ZipFile(self._path, "r") as zf:
            self._zip = zf
            self._names_in_zip = set(zf.namelist())

            db_entry = self._find_db()
            if not db_entry:
                log.error("userData.db not found in .jwlplaylist file")
                return

            db_bytes = self._zip.read(db_entry)

            # Reuse _parse_db's robust connection handling, but change the final extractor.
            # Pass a temporary dump function instead of returning self._extract.
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

            # Get all tables.
            tables = con.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()

            for table_row in tables:
                table_name = table_row["name"]
                lines.append("")
                lines.append(f"Table: {table_name}")

                # Read the columns from each table.
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


# Standalone function at the end of the file (near read_jwlplaylist)


def introspect_jwlplaylist(path: str | Path) -> None:
    """
    Open a .jwlplaylist file and print its actual database structure.
    Usage: introspect_jwlplaylist('my_playlist.jwlplaylist')
    """
    reader = JWLPlaylistReader(path)
    reader.print_schema()


# Convenience function


def read_jwlplaylist(
    path: str | Path,
    fallback_lang_code: str = "E",
    *,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> dict:
    """
    Simplified entry point.

    fallback_lang_code: LanguageManager api_code (e.g. "T" for pt_BR).
    Used for videos whose MEPS ID is absent from the known map.

    Return a dict with 'name' and 'items'.
    Propagate FileNotFoundError, BadZipFile, or ValueError.
    Cancellation is cooperative and propagates ``concurrent.futures.CancelledError``.
    Progress reports counts or uncompressed bytes according to the phase.
    """
    return JWLPlaylistReader(
        path,
        fallback_lang_code=fallback_lang_code,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
    ).parse()
