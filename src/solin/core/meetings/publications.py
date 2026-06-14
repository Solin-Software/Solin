"""
publications.py ─ Solin
==========================
Downloads, caches, extracts and parses .jwpub files for the Meetings widget.

Threading model (production-grade):
────────────────────────────────────
REGRA ABSOLUTA: nada que bloqueie (HTTP, disk I/O pesado, SQLite) roda na
main thread. Toda comunicação entre worker e UI é via Signal/Slot com
QueuedConnection automática (QThread garante a thread affinity correta).

_JwpubWorker   — QObject que vive num QThread dedicado.
                 Faz toda a lógica: fetch de URL, download, extração zip,
                 parse SQLite. Emite sinais de resultado para a main thread.
JwpubService   — QObject na main thread. Cria/gerencia o worker thread,
                 recebe sinais e repassa para a UI.

Isso elimina:
  • HTTP bloqueante na main thread (_get_jwpub_url era síncrono)
  • QRunnable + _Signals com moveToThread (AutoConnection frágil)
  • threading.Thread emitindo signals (sem QThread wrapper = DirectConnection)
  • resolve_video() síncrono chamado de _MediaRow no __init__
"""

from __future__ import annotations

import io
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject, QThread, Signal, Slot,
)

from solin.core.network.http import HttpError, stream_get
from solin.core.jw.publication_links import (
    DEFAULT_USER_AGENT,
    VIDEO_FORMATS,
    fetch_pub_media_json,
    select_pub_media_file,
)
from solin.core.media.cache import MediaCacheManager, is_url_cached
from solin.core.media.settings import MediaSettingsStore
from solin.core.storage.json_repository import JsonFileRepository

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_UA       = DEFAULT_USER_AGENT

SONG_SYMS  = {"sjj", "sjjm"}
_EXCL_CAT  = {9, 10, 15, 25}
_WT_CLASS  = 40
_MWB_AYFM  = 18
_SKIP_MEETING_REF_PUBS = {"th", "lmd"}
_MWB_STUDY_REF_TYPES = {"Book", "Brochure", "Watchtower", "Article"}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class MeetingMedia:
    multimedia_id:     int  = 0
    mime_type:         str  = ""
    file_path:         str  = ""
    label:             str  = ""
    caption:           str  = ""
    begin_ordinal:     int  = 0
    key_symbol:        str  = ""
    track:             int  = 0
    issue_tag:         int  = 0
    meps_doc_id:       int  = 0
    section:           str  = ""
    is_song:           bool = False
    cbs_article_title: str  = ""


@dataclass
class MeetingPublicationRef:
    section:           str  = ""
    begin_ordinal:     int  = 0
    pub:               str  = ""
    issue:             str  = "0"
    publication_title: str  = ""
    caption:           str  = ""
    meps_doc_id:       int  = 0
    is_cbs:            bool = False
    items:             list = field(default_factory=list)


@dataclass
class WeekData:
    monday:          date            = field(default_factory=date.today)
    mwb_pub_dir:     Optional[Path]  = None
    mwb_cover_bytes: Optional[bytes] = None
    mwb_date_label:  str             = ""
    mwb_week_title:  str             = ""
    mwb_all_media:   list            = field(default_factory=list)
    mwb_publication_refs: list       = field(default_factory=list)
    mwb_status:      str             = "idle"
    mwb_issue:       str             = ""
    wt_pub_dir:      Optional[Path]  = None
    wt_cover_bytes:  Optional[bytes] = None
    wt_study_title:  str             = ""
    wt_issue:        str             = ""
    wt_all_media:    list            = field(default_factory=list)
    wt_status:       str             = "idle"
    cbs_ref:         Optional[dict]  = None
    cbs_pub_dir:     Optional[Path]  = None
    cbs_items:       list            = field(default_factory=list)
    cbs_status:      str             = "idle"


# ── Filesystem cache ──────────────────────────────────────────────────────────

class JwpubCache:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def jwpub_path(self, pub: str, lang: str, issue: str) -> Path:
        d = self._root / f"{pub}_{lang}"
        d.mkdir(exist_ok=True)
        return d / f"{pub}_{lang}_{issue}.jwpub"

    def extract_dir(self, pub: str, lang: str, issue: str) -> Path:
        return self._root / f"{pub}_{lang}" / f"x_{issue}"

    def is_cached(self, pub: str, lang: str, issue: str) -> bool:
        ep = self.extract_dir(pub, lang, issue)
        return ep.exists() and any(ep.glob("*.db"))

    def db_path(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        ep  = self.extract_dir(pub, lang, issue)
        dbs = list(ep.glob("*.db"))
        return dbs[0] if dbs else None

    def invalidate_extract(self, pub: str, lang: str, issue: str) -> None:
        """
        Remove the extract directory for the given publication so that the
        next call to extract() or _ensure_extract() is forced to unpack the
        freshly-downloaded .jwpub instead of reusing stale files.

        Called immediately after a successful .jwpub write so that a checksum
        update always propagates to the extracted content.  Errors are logged
        but never raised — a missing or already-absent directory is a no-op.
        """
        ep = self.extract_dir(pub, lang, issue)
        if not ep.exists():
            return
        try:
            shutil.rmtree(ep)
            log.debug("JwpubCache: invalidated extract dir %s", ep)
        except OSError as exc:
            log.error("JwpubCache: could not remove extract dir %s: %s", ep, exc)

    def extract(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        jwpub = self.jwpub_path(pub, lang, issue)
        if not jwpub.exists():
            return None
        ep = self.extract_dir(pub, lang, issue)
        ep.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(jwpub, "r") as outer:
                if "contents" not in outer.namelist():
                    return None
                inner_bytes = outer.read("contents")
            with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner:
                inner.extractall(ep)
            return ep
        except (
            EOFError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            log.error("Extract failed %s: %s", jwpub, exc)
            return None


# ── Checksum persistence ──────────────────────────────────────────────────────

class JwpubChecksumStore:
    """
    Persists the MD5 checksums returned by the JW API alongside each .jwpub
    so that we can detect server-side content changes without re-downloading.

    Storage : the explicitly configured JSON file (atomic write via tmp+replace)
    Key     : "<pub>_<lang>_<issue>"  e.g. "mwb_T_202503", "w_T_202501"
    Value   : MD5 hex string from the API  e.g. "dcf8a1d77e8c804aece4efd0d47baa80"

    Thread safety
    -------------
    The application container shares one instance across every worker thread.
    All public methods are guarded by a lock, so concurrent saves from the
    meetings and Memorial workers are serialised and never overwrite each other.

    _flush() uses a read-merge-write strategy: it re-reads the file from disk
    inside the lock before writing, so that any entry persisted by a previous
    save() call is preserved even in the unlikely event that two separate
    instances exist within the same process (e.g. during hot-reload in dev).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._json = JsonFileRepository(path)
        self._json.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock  = threading.Lock()
        self._data: dict[str, str] = self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._json.exists():
                data = self._json.read()
                if isinstance(data, dict):
                    return data
                log.warning("ChecksumStore: unexpected format in %s — resetting", self._json.path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            log.warning("ChecksumStore: could not load %s: %s", self._json.path, exc)
        return {}

    def _flush(self) -> None:
        """
        Atomic read-merge-write so no concurrent save is lost.

        Strategy (all inside self._lock, already held by the caller):
          1. Re-read the on-disk file.
          2. Merge: disk entries first, then in-memory entries on top
             (in-memory wins on conflict — the current write is authoritative).
          3. Write the merged dict through the shared atomic JSON helper.
        """
        try:
            # 1. Read whatever is currently on disk (may have been written by
            #    another save() call that ran between our last _flush and now).
            on_disk: dict[str, str] = {}
            try:
                if self._json.exists():
                    raw = self._json.read()
                    if isinstance(raw, dict):
                        on_disk = raw
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                log.warning("ChecksumStore: re-read before flush failed: %s", exc)

            # 2. Merge: disk is the base; our in-memory view overwrites on conflict.
            merged = {**on_disk, **self._data}
            # Sync in-memory view so subsequent has_changed() calls are consistent.
            self._data = merged

            # 3. Atomic write.
            self._json.write(merged, sort_keys=True)
        except OSError as exc:
            log.error("ChecksumStore: could not save %s: %s", self._json.path, exc)

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def _key(pub: str, lang: str, issue: str) -> str:
        return f"{pub}_{lang}_{issue}"

    def get(self, pub: str, lang: str, issue: str) -> str:
        """Return the stored checksum, or '' if none recorded yet."""
        with self._lock:
            return self._data.get(self._key(pub, lang, issue), "")

    def save(self, pub: str, lang: str, issue: str, checksum: str) -> None:
        """Persist a checksum.  No-op when checksum is empty."""
        if not checksum:
            return
        with self._lock:
            self._data[self._key(pub, lang, issue)] = checksum
            self._flush()

    def has_changed(self, pub: str, lang: str, issue: str,
                    remote_checksum: str) -> bool:
        """
        Returns True when the remote checksum differs from the stored one,
        meaning the server-side file has been updated and we must re-download.

        Conservative: when remote_checksum is empty (API did not provide one)
        we assume nothing has changed and return False to avoid spurious
        re-downloads.
        """
        if not remote_checksum:
            return False
        with self._lock:
            stored = self._data.get(self._key(pub, lang, issue), "")
        changed = stored != remote_checksum
        if changed:
            log.info(
                "ChecksumStore: %s/%s/%s checksum changed (%s → %s)",
                pub, lang, issue, stored or "<none>", remote_checksum,
            )
        return changed

def _get_jwpub_info(pub: str, lang: str, issue: str) -> tuple[Optional[str], str, bool]:
    """
    Query the JW pub-media API and return (download_url, checksum, not_found).

    not_found=True  → The API responded successfully but has no files for this
                       pub/lang/issue.  The publication genuinely does not exist
                       (equivalent to an HTTP 404).
    not_found=False → Either a URL was found, or the request failed due to a
                       network / parse error (we cannot confirm existence).

    Both url and checksum come from the same API call; checksum is '' when the
    server does not supply one.  Returns (None, '', False) on any network error.
    """
    data = fetch_pub_media_json({
        "pub": pub, "issue": issue, "langwritten": lang,
        "fileformat": "JWPUB", "output": "json",
        "alllangs": "0", "txtCMSLang": "E",
    })
    if not data:
        return None, "", False   # network / parse error — cannot determine existence
    media_file = select_pub_media_file(data, lang, ("JWPUB",))
    if media_file is None:
        return None, "", True    # API replied but has no files → pub absent (404-equivalent)
    return media_file.url, media_file.checksum, False


def _resolve_video(key_symbol: str, track: int, issue_tag: int,
                   meps_doc_id: int, lang: str,
                   is_sign_language: bool = False) -> dict:
    """
    Resolve video URL — deve ser chamado APENAS de worker threads.

    is_sign_language: quando True e key_symbol for 'sjjm', substitui por 'sjj'
    (língua gestual não usa a versão com música).
    """
    # Centraliza a lógica sjj/sjjm: mesmo critério de jw.media_api.song_publication_symbol()
    if is_sign_language and (key_symbol or "").lower() == "sjjm":
        key_symbol = "sjj"

    result = {"url": "", "title": "", "thumbnail": ""}
    try:
        if key_symbol:
            params: dict = {
                "pub": key_symbol, "track": track,
                "langwritten": lang, "fileformat": "mp4,m4v",
                "output": "json", "alllangs": "0",
            }
            if issue_tag and int(issue_tag) != 0:
                params["issue"] = issue_tag
        elif meps_doc_id:
            params = {
                "docid": meps_doc_id, "langwritten": lang,
                "fileformat": "mp4,m4v", "output": "json", "alllangs": "0",
            }
        else:
            return result
        data = fetch_pub_media_json(params)
        if not data:
            return result
        media_file = select_pub_media_file(
            data,
            lang,
            VIDEO_FORMATS,
            prefer_highest_label=True,
        )
        if media_file is not None:
            result["url"] = media_file.url
            result["title"] = media_file.title
            result["thumbnail"] = media_file.thumbnail_url
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        log.debug("Could not parse resolved video metadata", exc_info=True)
    return result


# ── Date arithmetic ───────────────────────────────────────────────────────────

def _monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())

def _mwb_issue(monday: date) -> str:
    m = monday.month
    if m % 2 == 0:
        m -= 1
    return f"{monday.year}{m:02d}00"

def _wt_candidates(monday: date) -> list[str]:
    return [
        f"{(monday - timedelta(weeks=w)).year}"
        f"{(monday - timedelta(weeks=w)).month:02d}00"
        for w in [6, 8, 10, 12]
    ]


# ── SQLite helpers (worker-thread only) ──────────────────────────────────────

def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c

def _find_mwb_doc_id(conn, monday: date) -> Optional[int]:
    target = int(monday.strftime("%Y%m%d"))
    try:
        r = conn.execute(
            "SELECT DocumentId FROM DatedText WHERE FirstDateOffset=? LIMIT 1",
            (target,)
        ).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None

def _find_wt_doc_id(conn, monday: date) -> Optional[int]:
    target = int(monday.strftime("%Y%m%d"))
    try:
        rows   = conn.execute(
            "SELECT FirstDateOffset FROM DatedText ORDER BY FirstDateOffset"
        ).fetchall()
        week_nr = next((i for i, r in enumerate(rows) if r[0] == target), -1)
        if week_nr == -1:
            return None
        row = conn.execute(
            "SELECT DocumentId FROM Document WHERE Class=? "
            "ORDER BY DocumentId LIMIT 1 OFFSET ?",
            (_WT_CLASS, week_nr)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None

def _is_week_in_dated_text(conn, monday: date) -> bool:
    target = int(monday.strftime("%Y%m%d"))
    try:
        r = conn.execute(
            "SELECT COUNT(*) FROM DatedText WHERE FirstDateOffset=?", (target,)
        ).fetchone()
        return bool(r and r[0] > 0)
    except sqlite3.Error:
        return False

def _doc_title(conn, doc_id: int) -> str:
    try:
        r = conn.execute(
            "SELECT Title FROM Document WHERE DocumentId=?", (doc_id,)
        ).fetchone()
        return (r[0] or "").strip() if r else ""
    except sqlite3.Error:
        return ""

def _cover_bytes(pub_dir: Path) -> Optional[bytes]:
    dbs = list(pub_dir.glob("*.db"))
    if not dbs:
        return None
    try:
        c = _conn(dbs[0])
        for cat in (9, 15):
            r = c.execute(
                "SELECT FilePath FROM Multimedia WHERE CategoryType=? LIMIT 1", (cat,)
            ).fetchone()
            if r and r[0]:
                img = pub_dir / r[0]
                if img.exists():
                    c.close()
                    return img.read_bytes()
        c.close()
    except (OSError, sqlite3.Error):
        log.debug("Failed to read cover bytes from publication database", exc_info=True)
    return None

def _row_value(row, key: str) -> str:
    try:
        return row[key] or ""
    except (IndexError, KeyError, TypeError):
        return ""


def _clean_ref_text(value: str) -> str:
    import re as _re
    text = _re.sub(r"<[^>]+>", "", value or "")
    return _re.sub(r"\s+", " ", text).strip()


def _publication_title_from_row(row, fallback: str) -> str:
    for key in (
        "display_title",
        "reference_title",
        "short_title",
        "title",
        "undated_reference_title",
    ):
        title = _clean_ref_text(_row_value(row, key))
        if title:
            return title
    return fallback


def _mwb_song_ordinals(conn, doc_id: int) -> list[int]:
    try:
        return [
            int(r["par"] or 0) for r in conn.execute(
                "SELECT dm.BeginParagraphOrdinal AS par FROM Multimedia m "
                "JOIN DocumentMultimedia dm ON dm.MultimediaId=m.MultimediaId "
                "WHERE dm.DocumentId=? AND m.KeySymbol IN ('sjj','sjjm') "
                "AND dm.BeginParagraphOrdinal IS NOT NULL ORDER BY par",
                (doc_id,)
            ).fetchall()
        ]
    except sqlite3.Error:
        log.debug("Failed to read MWB song ordinals", exc_info=True)
        return []


def _mwb_mid_ordinal_from_songs(song_ords: list[int]) -> int:
    return song_ords[1] if len(song_ords) >= 2 else 9999


def _mwb_final_song_ordinal_from_songs(song_ords: list[int]) -> int:
    return song_ords[-1] if len(song_ords) >= 3 else 9999


def _mwb_mid_ordinal(conn, doc_id: int) -> int:
    return _mwb_mid_ordinal_from_songs(_mwb_song_ordinals(conn, doc_id))


def _mwb_final_song_ordinal(conn, doc_id: int) -> int:
    return _mwb_final_song_ordinal_from_songs(_mwb_song_ordinals(conn, doc_id))


def _is_cbs_row(row, cbs_ref: Optional[dict], final_song_ord: int = 9999) -> bool:
    if not cbs_ref:
        return False
    pub = str(row["undated"] or "").strip()
    meps_id = int(row["meps_id"] or 0)
    par = int(row["par"] or 0)
    cbs_docs = {int(value) for value in cbs_ref.get("meps_doc_ids", [])}
    if pub != str(cbs_ref.get("pub") or "") or meps_id not in cbs_docs:
        return False
    if par < int(cbs_ref.get("cbs_start") or 9999):
        return False
    return final_song_ord >= 9999 or par < final_song_ord


def _mwb_publication_ref_rows(conn, doc_id: int, include_web: bool = False) -> list:
    pub_types = "'Book','Brochure','Watchtower','Article'"
    if include_web:
        pub_types = f"{pub_types},'Web'"
    try:
        return conn.execute(f"""
            SELECT de.BeginParagraphOrdinal AS par,
                   e.Caption,
                   e.RefMepsDocumentId       AS meps_id,
                   rp.UndatedSymbol          AS undated,
                   rp.IssueTagNumber         AS issue_tag,
                   rp.PublicationType        AS pub_type,
                   rp.DisplayTitle           AS display_title,
                   rp.ReferenceTitle         AS reference_title,
                   rp.ShortTitle             AS short_title,
                   rp.Title                  AS title,
                   rp.UndatedReferenceTitle  AS undated_reference_title
            FROM   DocumentExtract de
            JOIN   Extract e         ON e.ExtractId         = de.ExtractId
            JOIN   RefPublication rp ON rp.RefPublicationId = e.RefPublicationId
            WHERE  de.DocumentId = ?
              AND  rp.UndatedSymbol NOT IN ('sjj','sjjm')
              AND  rp.PublicationType IN ({pub_types})
            ORDER  BY de.BeginParagraphOrdinal ASC
        """, (doc_id,)).fetchall()
    except sqlite3.Error:
        try:
            return conn.execute(f"""
                SELECT de.BeginParagraphOrdinal AS par,
                       e.Caption,
                       e.RefMepsDocumentId       AS meps_id,
                       rp.UndatedSymbol          AS undated,
                       rp.IssueTagNumber         AS issue_tag,
                       rp.PublicationType        AS pub_type,
                       NULL                      AS display_title,
                       NULL                      AS reference_title,
                       NULL                      AS short_title,
                       NULL                      AS title,
                       NULL                      AS undated_reference_title
                FROM   DocumentExtract de
                JOIN   Extract e         ON e.ExtractId         = de.ExtractId
                JOIN   RefPublication rp ON rp.RefPublicationId = e.RefPublicationId
                WHERE  de.DocumentId = ?
                  AND  rp.UndatedSymbol NOT IN ('sjj','sjjm')
                  AND  rp.PublicationType IN ({pub_types})
                ORDER  BY de.BeginParagraphOrdinal ASC
            """, (doc_id,)).fetchall()
        except sqlite3.Error:
            log.debug("Failed to read MWB publication references", exc_info=True)
            return []


def _mwb_study_ref_rows(rows: list) -> list:
    return [
        row for row in rows
        if str(row["pub_type"] or "") in _MWB_STUDY_REF_TYPES
    ]


def _infer_cbs_rows(rows: list, final_song_ord: int) -> list:
    candidates = [
        r for r in rows
        if (r["undated"] or "").strip()
        and (final_song_ord >= 9999 or int(r["par"] or 0) < final_song_ord)
    ]
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda r: int(r["par"] or 0), reverse=True)
    top_sym = str(ordered[0]["undated"] or "").strip()
    cbs_rows = []
    last_cbs_par = int(ordered[0]["par"] or 0)
    for row in ordered:
        pub = str(row["undated"] or "").strip()
        par = int(row["par"] or 0)
        if pub == top_sym:
            cbs_rows.append(row)
            last_cbs_par = par
            continue
        if cbs_rows and par < last_cbs_par:
            break
    return sorted(cbs_rows, key=lambda r: int(r["par"] or 0))


def _mwb_cbs_start_from_rows(rows: list, final_song_ord: int) -> int:
    rows = _infer_cbs_rows(
        _mwb_study_ref_rows(rows),
        final_song_ord,
    )
    if rows:
        return min(int(row["par"] or 9999) for row in rows)
    return 9999


def _mwb_cbs_start(conn, doc_id: int) -> int:
    song_ords = _mwb_song_ordinals(conn, doc_id)
    return _mwb_cbs_start_from_rows(
        _mwb_publication_ref_rows(conn, doc_id),
        _mwb_final_song_ordinal_from_songs(song_ords),
    )


def _mwb_section_for_par(par: int, mid_ord: int, cbs_start: int) -> str:
    if cbs_start < 9999 and par >= cbs_start:
        return "lac"
    if par >= mid_ord:
        return "lac"
    if par >= _MWB_AYFM:
        return "ayfm"
    return "tgw"


def _get_cbs_ref(
    conn,
    doc_id: int,
    rows: Optional[list] = None,
    final_song_ord: Optional[int] = None,
) -> Optional[dict]:
    if rows is None:
        rows = _mwb_publication_ref_rows(conn, doc_id)
    if final_song_ord is None:
        final_song_ord = _mwb_final_song_ordinal(conn, doc_id)
    cbs_rows = _infer_cbs_rows(_mwb_study_ref_rows(rows), final_song_ord)
    if not cbs_rows:
        return None

    top_row   = max(cbs_rows, key=lambda r: r["par"])
    top_sym   = top_row["undated"] or ""
    top_issue = str(top_row["issue_tag"] or "0")
    if not top_sym:
        return None
    pub_title = ""
    for key in (
        "display_title",
        "reference_title",
        "short_title",
        "title",
        "undated_reference_title",
    ):
        pub_title = _clean_ref_text(_row_value(top_row, key))
        if pub_title:
            break
    meps_ids:   list[int]      = []
    doc_titles: dict[int, str] = {}
    cbs_start = min(int(row["par"] or 0) for row in cbs_rows)
    for r in cbs_rows:
        if (r["undated"] or "") != top_sym:
            continue
        mid = r["meps_id"]
        if not mid:
            continue
        if mid not in meps_ids:
            meps_ids.append(mid)
        if mid not in doc_titles and r["Caption"]:
            doc_titles[mid] = _clean_ref_text(r["Caption"])
    if not meps_ids:
        return None
    return {
        "pub":          top_sym,
        "issue":        top_issue,
        "publication_title": pub_title or top_sym,
        "meps_doc_ids": meps_ids,
        "doc_titles":   doc_titles,
        "title":        doc_titles.get(meps_ids[0], ""),
        "cbs_start":    cbs_start,
    }


def _get_mwb_publication_refs(
    conn,
    doc_id: int,
    cbs_ref: Optional[dict] = None,
    rows: Optional[list] = None,
    mid_ord: Optional[int] = None,
    final_song_ord: Optional[int] = None,
    cbs_start: Optional[int] = None,
) -> list[MeetingPublicationRef]:
    if rows is None:
        rows = _mwb_publication_ref_rows(conn, doc_id, include_web=True)
    if not rows:
        return []

    if final_song_ord is None:
        final_song_ord = _mwb_final_song_ordinal(conn, doc_id)
    cbs_ref = cbs_ref or _get_cbs_ref(conn, doc_id, rows, final_song_ord)
    if mid_ord is None:
        mid_ord = _mwb_mid_ordinal(conn, doc_id)
    if cbs_start is None:
        cbs_start = int(
            (cbs_ref or {}).get("cbs_start")
            or _mwb_cbs_start_from_rows(rows, final_song_ord)
        )

    refs_by_key: dict[tuple[str, str, str, int, bool], MeetingPublicationRef] = {}
    order: list[tuple[str, str, str, int, bool]] = []
    for row in rows:
        pub = str(row["undated"] or "").strip()
        meps_id = int(row["meps_id"] or 0)
        if not pub or pub.lower() in _SKIP_MEETING_REF_PUBS or not meps_id:
            continue
        par = int(row["par"] or 0)
        issue = str(row["issue_tag"] or "0")
        section = _mwb_section_for_par(par, mid_ord, cbs_start)
        is_cbs = _is_cbs_row(row, cbs_ref, final_song_ord)
        key = (section, pub, issue, meps_id, is_cbs)
        caption = _clean_ref_text(row["Caption"] or "")
        if key in refs_by_key:
            ref = refs_by_key[key]
            ref.begin_ordinal = min(ref.begin_ordinal, par)
            if len(caption) > len(ref.caption):
                ref.caption = caption
            continue
        refs_by_key[key] = MeetingPublicationRef(
            section=section,
            begin_ordinal=par,
            pub=pub,
            issue=issue,
            publication_title=_publication_title_from_row(row, pub),
            caption=caption,
            meps_doc_id=meps_id,
            is_cbs=is_cbs,
        )
        order.append(key)
    return sorted((refs_by_key[key] for key in order), key=lambda ref: ref.begin_ordinal)


def _excl_str() -> str:
    return ",".join(str(c) for c in _EXCL_CAT)

def _query_multimedia(conn, doc_id: int) -> list:
    """
    Return one row per MultimediaId for this document.

    Sign-language publications (e.g. LGP) often associate the *same*
    MultimediaId with several paragraphs in DocumentMultimedia — one video
    covers a range of paragraphs.  Without GROUP BY the plain JOIN would
    return duplicate rows for each paragraph association, causing every video
    to appear N times in the media list.

    We group by MultimediaId and take the *minimum* BeginParagraphOrdinal so
    that section-assignment logic (_parse_mwb_media) still places the item at
    its natural position in the meeting outline.
    """
    excl = _excl_str()
    for clause in ["AND m.SuppressZoom IS NOT 1", ""]:
        try:
            return conn.execute(f"""
                SELECT m.MultimediaId, m.FilePath, m.MimeType, m.CategoryType,
                       m.Label, m.Caption, m.KeySymbol, m.Track,
                       m.IssueTagNumber, m.MepsDocumentId,
                       MIN(dm.BeginParagraphOrdinal) AS par
                FROM   Multimedia m
                JOIN   DocumentMultimedia dm ON dm.MultimediaId=m.MultimediaId
                WHERE  dm.DocumentId=?
                  AND  m.CategoryType NOT IN ({excl})
                  AND  dm.BeginParagraphOrdinal IS NOT NULL
                  {clause}
                GROUP BY m.MultimediaId
                ORDER  BY par
            """, (doc_id,)).fetchall()
        except sqlite3.OperationalError:
            continue
    return []

def _media_file_path(raw_path: str, pub_dir: Path) -> str:
    raw_path = str(raw_path or "").strip()
    if not raw_path:
        return ""
    if raw_path.startswith(("http://", "https://")):
        return raw_path

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = pub_dir / raw_path
    if candidate.exists():
        return str(candidate)
    return ""


def _make_media_item(r, pub_dir: Path, section: str,
                     is_song: bool, cbs_title: str = "") -> MeetingMedia:
    mime   = (r["MimeType"] or "").lower()
    fp     = r["FilePath"] or ""
    file_path = _media_file_path(fp, pub_dir)
    return MeetingMedia(
        multimedia_id     = r["MultimediaId"],
        mime_type         = mime,
        file_path         = file_path,
        label             = r["Label"] or "",
        caption           = r["Caption"] or "",
        begin_ordinal     = r["par"] or 0,
        key_symbol        = r["KeySymbol"] or "",
        track             = r["Track"] or 0,
        issue_tag         = r["IssueTagNumber"] or 0,
        meps_doc_id       = r["MepsDocumentId"] or 0,
        section           = section,
        is_song           = is_song,
        cbs_article_title = cbs_title,
    )

def _dedup_multimedia_rows(rows: list) -> list:
    """
    Remove rows that would resolve to the same video/audio.

    In sign-language publications a single meeting section can contain several
    Multimedia rows with *different* MultimediaIds but the *same* resolution
    key (KeySymbol+Track, or MepsDocumentId).  All of them hit the same CDN
    URL, so the user would see the exact same video card repeated.

    Strategy (mirrors jw.publication_reader):
      1. Deduplicate image rows by FilePath — same file shown once.
      2. Deduplicate video/audio rows by (KeySymbol, Track, IssueTagNumber)
         when KeySymbol is present, or by MepsDocumentId otherwise.
      3. Rows that have neither key are kept as-is (edge case).

    Ordering from the caller (_query_multimedia already groups by MIN par) is
    preserved — the first occurrence wins.
    """
    seen_mid:     set[int]   = set()
    seen_vid_key: set[tuple] = set()
    seen_img:     set[str]   = set()
    result = []
    for r in rows:
        mid  = r["MultimediaId"]
        if mid in seen_mid:
            continue
        seen_mid.add(mid)

        mime = (r["MimeType"] or "").lower()
        if mime.startswith("image"):
            fp = r["FilePath"] or ""
            if fp and fp in seen_img:
                continue
            if fp:
                seen_img.add(fp)
        else:
            sym   = (r["KeySymbol"] or "").strip()
            track = r["Track"]
            issue = r["IssueTagNumber"]
            meps  = r["MepsDocumentId"]
            if sym:
                vk = (sym.lower(), track, issue)
            elif meps:
                vk = ("__meps__", meps, None)
            else:
                vk = None
            if vk is not None:
                if vk in seen_vid_key:
                    continue
                seen_vid_key.add(vk)

        result.append(r)
    return result


def _parse_mwb_media(
    conn,
    doc_id: int,
    pub_dir: Path,
    mid_ord: Optional[int] = None,
    cbs_start: Optional[int] = None,
) -> list[MeetingMedia]:
    if mid_ord is None:
        mid_ord = _mwb_mid_ordinal(conn, doc_id)
    if cbs_start is None:
        cbs_start = _mwb_cbs_start(conn, doc_id)
    result = []
    for r in _dedup_multimedia_rows(_query_multimedia(conn, doc_id)):
        par     = r["par"] or 0
        sym     = (r["KeySymbol"] or "").lower()
        is_song = sym in SONG_SYMS
        section = _mwb_section_for_par(par, mid_ord, cbs_start)
        result.append(_make_media_item(r, pub_dir, section, is_song))
    return result

def _parse_wt_media(conn, doc_id: int, pub_dir: Path) -> list[MeetingMedia]:
    return [
        _make_media_item(r, pub_dir, "wt", (r["KeySymbol"] or "").lower() in SONG_SYMS)
        for r in _dedup_multimedia_rows(_query_multimedia(conn, doc_id))
    ]

def _parse_publication_ref_items(
    pub_dir: Path,
    db_path: Path,
    meps_doc_id: int,
    section: str,
    marker_title: str,
) -> list[MeetingMedia]:
    result = []
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT DocumentId FROM Document WHERE MepsDocumentId=? LIMIT 1",
            (meps_doc_id,)
        ).fetchone()
        if not row:
            conn.close()
            return []
        for r in _dedup_multimedia_rows(_query_multimedia(conn, row[0])):
            sym = (r["KeySymbol"] or "").lower()
            result.append(_make_media_item(
                r, pub_dir, section, sym in SONG_SYMS, marker_title
            ))
        conn.close()
    except (IndexError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        log.exception("Could not parse publication reference media")
    return result


def _sync_cbs_from_publication_refs(wd: WeekData) -> None:
    refs = [
        ref for ref in getattr(wd, "mwb_publication_refs", []) or []
        if getattr(ref, "is_cbs", False)
    ]
    if not refs:
        wd.cbs_items = []
        return

    refs = sorted(refs, key=lambda ref: int(ref.begin_ordinal or 0))
    items: list[MeetingMedia] = []
    meps_doc_ids: list[int] = []
    doc_titles: dict[int, str] = {}
    for ref in refs:
        meps_doc_id = int(ref.meps_doc_id or 0)
        if meps_doc_id and meps_doc_id not in meps_doc_ids:
            meps_doc_ids.append(meps_doc_id)
        if meps_doc_id and ref.caption:
            doc_titles[meps_doc_id] = ref.caption
        items.extend(ref.items or [])

    first = refs[0]
    if wd.cbs_ref:
        existing_ids = [
            int(value) for value in wd.cbs_ref.get("meps_doc_ids", [])
            if int(value or 0)
        ]
        existing_titles = {
            int(key): value
            for key, value in (wd.cbs_ref.get("doc_titles") or {}).items()
            if int(key or 0)
        }
        for meps_doc_id in meps_doc_ids:
            if meps_doc_id not in existing_ids:
                existing_ids.append(meps_doc_id)
            if meps_doc_id in doc_titles and meps_doc_id not in existing_titles:
                existing_titles[meps_doc_id] = doc_titles[meps_doc_id]
        wd.cbs_ref = dict(wd.cbs_ref)
        wd.cbs_ref["issue"] = first.issue or wd.cbs_ref.get("issue", "0")
        wd.cbs_ref["meps_doc_ids"] = existing_ids
        wd.cbs_ref["doc_titles"] = existing_titles
        wd.cbs_ref["title"] = (
            wd.cbs_ref.get("title")
            or (existing_titles.get(existing_ids[0], "") if existing_ids else "")
        )
    else:
        wd.cbs_ref = {
            "pub": first.pub,
            "issue": first.issue or "0",
            "publication_title": first.publication_title or first.pub,
            "meps_doc_ids": meps_doc_ids,
            "doc_titles": doc_titles,
            "title": doc_titles.get(meps_doc_ids[0], "") if meps_doc_ids else "",
            "cbs_start": first.begin_ordinal,
        }
    wd.cbs_items = items


# ── Worker — toda lógica bloqueante aqui, nunca na main thread ────────────────

class _JwpubWorker(QObject):
    """
    Roda num QThread dedicado. Toda operação bloqueante (HTTP, zip, SQLite)
    acontece aqui. Comunica com JwpubService exclusivamente via sinais —
    QThread garante QueuedConnection automática, sem moveToThread manual.

    Sinais emitidos para a main thread:
      mwb_done(key, WeekData)
      wt_done(key, WeekData)
      cbs_done(key, WeekData)
      progress(key, pub, pct)
      error(key, pub, msg)
      video_resolved(request_id, url, title, thumbnail)
      prefetch_requested(url)   — pede ao JwpubService para iniciar prefetch
    """
    mwb_done          = Signal(str, object)
    wt_done           = Signal(str, object)
    cbs_done          = Signal(str, object)
    progress          = Signal(str, str, int)
    error             = Signal(str, str, str)
    video_resolved    = Signal(str, str, str, str)   # request_id, url, title, thumb
    prefetch_requested = Signal(str)                  # url para prefetch

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._cache           = JwpubCache(jwpub_cache_dir)
        self._checksum_store  = checksum_store
        self._lang            = "T"
        self._is_sign_language = False

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(bool)
    def set_sign_language(self, is_sign: bool):
        """Informa se o idioma de mídia é gestual (afeta resolução de cânticos)."""
        self._is_sign_language = is_sign

    # ── Checksum-aware download gate ──────────────────────────────────────────

    def _needs_download(self, pub: str, lang: str, issue: str,
                        checksum: str, force: bool) -> bool:
        """
        Returns True when the file must be (re-)downloaded:
          • no local extract/archive exists yet
          • checksum changed compared with the real local archive
        Returns False when the cached copy is confirmed up-to-date.
        """
        has_extract = self._cache.is_cached(pub, lang, issue)
        archive = self._cache.jwpub_path(pub, lang, issue)
        has_archive = archive.is_file()

        if not has_extract and not has_archive:
            return True
        if not checksum:
            return False

        stored = self._checksum_store.get(pub, lang, issue)
        if stored == checksum:
            return False

        local_checksum = self._local_jwpub_checksum(archive) if has_archive else ""
        if local_checksum and local_checksum == checksum:
            self._checksum_store.save(pub, lang, issue, checksum)
            return False

        if not stored and has_extract and not has_archive:
            # Legacy cache: extracted DB exists but the original archive/checksum
            # does not. Trust the local usable cache and seed the remote checksum
            # so future launches do not redownload forever.
            self._checksum_store.save(pub, lang, issue, checksum)
            return False

        return True

    def _local_jwpub_checksum(self, path: Path) -> str:
        try:
            digest = hashlib.md5()  # nosec B324 - JW API exposes MD5 checksums
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    # ── Load week ─────────────────────────────────────────────────────────────

    @Slot(object, bool)
    def load_week(self, monday: date, force: bool = False):
        """
        Entry point: carrega MWB + WT para a semana dada.

        Stale-while-revalidate (SWR)
        ────────────────────────────
        Fase 1 — SERVIR: se houver cópia local utilizável, renderiza as duas
                 reuniões IMEDIATAMENTE, sem tocar na rede. É isto que torna um
                 launch "quente" instantâneo — nunca bloqueamos a tela só para
                 confirmar um arquivo que não mudou.
        Fase 2 — REVALIDAR: consulta a API em segundo plano (thread do worker).
                 Só baixa e re-emite quando o checksum do servidor realmente
                 mudou. Se nada mudou, nada acontece (sem segundo emit).

        force=True (retry de erro / troca de idioma) pula a fase de servir e faz
        uma revalidação limpa, mantendo a semântica de "recarregar de verdade".
        """
        mwb_served = False
        wt_served: Optional[str] = None
        if not force:
            mwb_served = self._serve_mwb_cached(monday)
            wt_served  = self._serve_wt_cached(monday)
        self._revalidate_mwb(monday, force, mwb_served)
        self._revalidate_wt(monday, force, wt_served)

    # ── MWB ───────────────────────────────────────────────────────────────────

    def _serve_mwb_cached(self, monday: date) -> bool:
        """Fase 1 (SWR): renderiza o MWB do cache local sem rede. True se servido."""
        issue = _mwb_issue(monday)
        lang  = self._lang
        if not self._cache.is_cached("mwb", lang, issue):
            return False
        self._parse_mwb(
            WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
            monday, issue, lang,
        )
        return True

    def _revalidate_mwb(self, monday: date, force: bool, served: bool):
        """
        Fase 2 (SWR): consulta a API e só re-emite se o conteúdo do servidor
        mudou. ``served`` indica se a fase 1 já mostrou uma cópia do cache.
        """
        key   = monday.isoformat()
        issue = _mwb_issue(monday)
        lang  = self._lang

        url, checksum, not_found = _get_jwpub_info("mwb", lang, issue)

        if not url:
            # API inalcançável, ou a publicação não existe para esta semana/idioma.
            if served:
                return  # já mostramos o cache — nada a fazer
            if self._cache.is_cached("mwb", lang, issue):
                log.warning("mwb %s: API unreachable, falling back to cached copy", issue)
                self._parse_mwb(
                    WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
                    monday, issue, lang,
                )
                return
            self.error.emit(
                key, "mwb", "NOT_FOUND" if not_found else f"No URL for mwb {issue}"
            )
            return

        if not self._needs_download("mwb", lang, issue, checksum, force):
            # Cópia local confirmada atual. Se já a servimos, "nada acontece".
            if not served:
                self._parse_mwb(
                    WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
                    monday, issue, lang,
                )
            return

        # Conteúdo mudou no servidor (ou nada em cache ainda) → baixa e re-renderiza.
        if not self._download("mwb", lang, issue, url, key, "mwb", emit_error=not served):
            return  # falhou; se já servimos cache, ele permanece na tela
        self._checksum_store.save("mwb", lang, issue, checksum)
        self._parse_mwb(
            WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
            monday, issue, lang,
        )

    def _parse_mwb(self, wd: WeekData, monday: date, issue: str, lang: str):
        key     = monday.isoformat()
        pub_dir = self._ensure_extract("mwb", lang, issue)
        db_path = self._cache.db_path("mwb", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.mwb_status = "error"
            self.error.emit(key, "mwb", "Extraction failed")
            return
        try:
            conn = _conn(db_path)
            if not _is_week_in_dated_text(conn, monday):
                conn.close()
                wd.mwb_status = "empty"
                self.mwb_done.emit(key, wd)
                return
            doc_id = _find_mwb_doc_id(conn, monday)
            if doc_id is None:
                conn.close()
                wd.mwb_status = "empty"
                self.mwb_done.emit(key, wd)
                return
            date_label = _doc_title(conn, doc_id)
            song_ords = _mwb_song_ordinals(conn, doc_id)
            mid_ord = _mwb_mid_ordinal_from_songs(song_ords)
            final_song_ord = _mwb_final_song_ordinal_from_songs(song_ords)
            publication_rows = _mwb_publication_ref_rows(
                conn,
                doc_id,
                include_web=True,
            )
            cbs_ref = _get_cbs_ref(
                conn,
                doc_id,
                publication_rows,
                final_song_ord,
            )
            cbs_start = int(
                (cbs_ref or {}).get("cbs_start")
                or _mwb_cbs_start_from_rows(publication_rows, final_song_ord)
            )
            items = _parse_mwb_media(conn, doc_id, pub_dir, mid_ord, cbs_start)
            publication_refs = _get_mwb_publication_refs(
                conn,
                doc_id,
                cbs_ref,
                publication_rows,
                mid_ord,
                final_song_ord,
                cbs_start,
            )
            conn.close()
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse MWB publication %s/%s", lang, issue)
            wd.mwb_status = "error"
            self.error.emit(key, "mwb", str(exc))
            return

        wd.mwb_pub_dir     = pub_dir
        wd.mwb_date_label  = date_label
        wd.mwb_week_title  = date_label
        wd.mwb_all_media   = items
        wd.mwb_publication_refs = publication_refs
        wd.mwb_cover_bytes = _cover_bytes(pub_dir)
        wd.mwb_status      = "ready"
        wd.cbs_ref         = cbs_ref
        _sync_cbs_from_publication_refs(wd)
        if publication_refs:
            wd.cbs_status = "loading"
        self.mwb_done.emit(key, wd)
        if publication_refs:
            self._load_mwb_publication_refs(monday, wd, publication_refs)

    # ── WT ────────────────────────────────────────────────────────────────────

    def _serve_wt_cached(self, monday: date) -> Optional[str]:
        """
        Fase 1 (SWR): renderiza o WT do cache local sem rede.

        A edição de A Sentinela que contém a semana de estudo não é determinística
        (a edição de um mês pode conter estudos de outro), então tentamos cada
        candidato em ordem e servimos o PRIMEIRO que já está em cache E contém a
        semana. Retorna a edição servida, ou None se nenhuma cópia local serve.
        """
        lang = self._lang
        for issue in _wt_candidates(monday):
            if not self._cache.is_cached("w", lang, issue):
                continue
            wd = WeekData(monday=monday, wt_status="loading")
            if self._try_wt_cached(wd, monday, issue, lang):
                return issue
        return None

    def _revalidate_wt(self, monday: date, force: bool, served_issue: Optional[str]):
        """
        Fase 2 (SWR). Se já servimos uma edição do cache, revalida APENAS ela
        (a única que importa) e só re-emite se mudou. Caso contrário, cai no
        resolvedor frio que sonda os candidatos pela rede e baixa o correto.
        """
        lang = self._lang
        if served_issue is not None:
            key = monday.isoformat()
            url, checksum, _ = _get_jwpub_info("w", lang, served_issue)
            if url and self._needs_download("w", lang, served_issue, checksum, force):
                if self._download("w", lang, served_issue, url, key, "wt", emit_error=False):
                    self._checksum_store.save("w", lang, served_issue, checksum)
                    self._try_wt_cached(
                        WeekData(monday=monday, wt_status="loading"),
                        monday, served_issue, lang,
                    )
            return
        # Caminho frio: sem cache utilizável → sonda candidatos e baixa pela rede.
        self._download_wt_chain(monday, lang, _wt_candidates(monday)[:], force=force)

    def _try_wt_cached(self, wd: WeekData, monday: date,
                        issue: str, lang: str) -> bool:
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            return False
        try:
            conn   = _conn(db_path)
            doc_id = _find_wt_doc_id(conn, monday)
            conn.close()
            if doc_id is None:
                return False
        except (OSError, sqlite3.Error):
            return False
        self._parse_wt(wd, monday, issue, lang)
        return True

    def _download_wt_chain(
        self,
        monday: date,
        lang: str,
        candidates: list[str],
        _had_api_response: bool = False,
        *,
        force: bool = False,
    ):
        """
        Try each WT issue candidate in order.  Each candidate is independently
        cache/ checksum gated so a valid local issue is never downloaded again.

        When all candidates are exhausted:
          • _had_api_response=True  → at least one API call replied but had no files
                                       → emit NOT_FOUND (pub absent for this week)
          • _had_api_response=False → all calls failed with network errors
                                       → emit generic error (connectivity problem)
        """
        key = monday.isoformat()
        had_api = _had_api_response

        for issue in candidates:
            is_cached = self._cache.is_cached("w", lang, issue)
            url, checksum, not_found = _get_jwpub_info("w", lang, issue)
            had_api = had_api or not_found or bool(url)

            if not url:
                if is_cached:
                    log.warning(
                        "wt %s: API unreachable, falling back to cached copy",
                        issue,
                    )
                    wd = WeekData(monday=monday, wt_status="loading")
                    if self._try_wt_cached(wd, monday, issue, lang):
                        return
                continue

            if self._needs_download("w", lang, issue, checksum, force):
                if not self._download("w", lang, issue, url, key, "wt"):
                    continue
                self._checksum_store.save("w", lang, issue, checksum)

            wd = WeekData(monday=monday, wt_status="loading")
            if self._try_wt_cached(wd, monday, issue, lang):
                return

        wd = WeekData(monday=monday, wt_status="not_found" if had_api else "error")
        msg = "NOT_FOUND" if had_api else "No WT issue found for this week"
        self.error.emit(key, "wt", msg)

    def _parse_wt(self, wd: WeekData, monday: date, issue: str, lang: str):
        key     = monday.isoformat()
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.wt_status = "error"
            self.error.emit(key, "wt", "Extraction failed")
            return
        try:
            conn   = _conn(db_path)
            doc_id = _find_wt_doc_id(conn, monday)
            if doc_id is None:
                conn.close()
                wd.wt_status = "empty"
                self.wt_done.emit(key, wd)
                return
            title = _doc_title(conn, doc_id)
            items = _parse_wt_media(conn, doc_id, pub_dir)
            conn.close()
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse WT publication %s/%s", lang, issue)
            wd.wt_status = "error"
            self.error.emit(key, "wt", str(exc))
            return
        wd.wt_pub_dir     = pub_dir
        wd.wt_issue       = issue
        wd.wt_study_title = title
        wd.wt_all_media   = items
        wd.wt_cover_bytes = _cover_bytes(pub_dir)
        wd.wt_status      = "ready"
        self.wt_done.emit(key, wd)

    def _sync_loaded_publication_refs(self, wd: WeekData, lang: str) -> None:
        _sync_cbs_from_publication_refs(wd)
        cbs_ref = next(
            (
                ref for ref in (wd.mwb_publication_refs or [])
                if getattr(ref, "is_cbs", False)
            ),
            None,
        )
        if cbs_ref:
            wd.cbs_pub_dir = self._cache.extract_dir(
                cbs_ref.pub,
                lang,
                cbs_ref.issue or "0",
            )

    def _load_mwb_publication_refs(
        self,
        monday: date,
        wd: WeekData,
        refs: list[MeetingPublicationRef],
    ):
        key = monday.isoformat()
        lang = self._lang

        def build(cache_only: bool) -> tuple[list[MeetingPublicationRef], bool]:
            out: list[MeetingPublicationRef] = []
            downloaded = False
            for ref in refs:
                items, did_dl = self._load_publication_ref_items(
                    key, lang, ref, cache_only=cache_only
                )
                downloaded = downloaded or did_dl
                if items:
                    ref.items = items
                    out.append(ref)
            return out, downloaded

        # ── Fase 1 (SWR): monta as referências do cache, sem rede ─────────────
        cached, _ = build(cache_only=True)
        if cached:
            wd.mwb_publication_refs = list(cached)
            self._sync_loaded_publication_refs(wd, lang)
            wd.cbs_status = "ready"
            self.cbs_done.emit(key, wd)

        # ── Fase 2 (SWR): revalida/baixa; só re-emite se algo mudou ───────────
        loaded, downloaded = build(cache_only=False)
        if loaded and (downloaded or len(loaded) != len(cached)):
            wd.mwb_publication_refs = loaded
            self._sync_loaded_publication_refs(wd, lang)
            wd.cbs_status = "ready"
            self.cbs_done.emit(key, wd)

    def _load_publication_ref_items(
        self,
        key: str,
        lang: str,
        ref: MeetingPublicationRef,
        cache_only: bool = False,
    ) -> tuple[list[MeetingMedia], bool]:
        """
        Resolve os itens de mídia de uma referência de publicação.

        cache_only=True  → fase de servir (SWR): usa só o cache local, sem rede.
        cache_only=False → fase de revalidar: consulta a API e baixa se mudou.

        Retorna (items, downloaded) — downloaded indica se um arquivo novo foi
        baixado nesta chamada (usado para decidir se re-emitir o lote).
        """
        pub = ref.pub
        issue = ref.issue or "0"

        if cache_only:
            for cand in ([issue] if issue == "0" else [issue, "0"]):
                if self._cache.is_cached(pub, lang, cand):
                    if cand != issue:
                        ref.issue = cand
                    return self._parse_ref_items(pub, lang, cand, ref), False
            return [], False

        url, checksum, _ = _get_jwpub_info(pub, lang, issue)
        if not url and issue != "0":
            fallback_url, fallback_checksum, _ = _get_jwpub_info(pub, lang, "0")
            if fallback_url or self._cache.is_cached(pub, lang, "0"):
                url = fallback_url
                checksum = fallback_checksum
                issue = "0"
                ref.issue = "0"

        is_cached = self._cache.is_cached(pub, lang, issue)
        if not url and not is_cached:
            return [], False

        downloaded = False
        if url and self._needs_download(pub, lang, issue, checksum, False):
            if not self._download(pub, lang, issue, url, key, "mwb", emit_error=False):
                return [], False
            self._checksum_store.save(pub, lang, issue, checksum)
            downloaded = True
        elif not url and is_cached:
            log.warning(
                "mwb ref %s/%s: API unreachable, using stale cache", pub, issue
            )

        return self._parse_ref_items(pub, lang, issue, ref), downloaded

    def _parse_ref_items(
        self, pub: str, lang: str, issue: str, ref: MeetingPublicationRef
    ) -> list[MeetingMedia]:
        pub_dir = self._ensure_extract(pub, lang, issue)
        db_path = self._cache.db_path(pub, lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            return []
        return _parse_publication_ref_items(
            pub_dir,
            db_path,
            ref.meps_doc_id,
            "cbs" if ref.is_cbs else ref.section,
            ref.caption,
        )

    # ── Video URL resolution (async, called from main thread via signal) ──────

    @Slot(str, str, int, int, int, str)
    def resolve_video_async(self, request_id: str, key_symbol: str,
                             track: int, issue_tag: int,
                             meps_doc_id: int, lang: str):
        """Resolve URL de vídeo em background. Resultado via video_resolved signal."""
        result = _resolve_video(key_symbol, track, issue_tag, meps_doc_id, lang,
                                is_sign_language=self._is_sign_language)
        self.video_resolved.emit(
            request_id,
            result.get("url", ""),
            result.get("title", ""),
            result.get("thumbnail", ""),
        )

    # ── Auto-prefetch ─────────────────────────────────────────────────────────

    @Slot(object)
    def prefetch_week_media(self, wd: WeekData):
        """Resolve URLs e emite prefetch_requested para cada item não cacheado."""
        all_items = list(wd.mwb_all_media) + list(wd.wt_all_media)
        ref_items: list[MeetingMedia] = []
        for ref in getattr(wd, "mwb_publication_refs", []):
            ref_items.extend(getattr(ref, "items", []) or [])
        all_items.extend(ref_items or list(wd.cbs_items))
        for item in all_items:
            if "image" in (item.mime_type or ""):
                continue
            resolved = _resolve_video(
                item.key_symbol, item.track, item.issue_tag,
                item.meps_doc_id, self._lang,
                is_sign_language=self._is_sign_language,
            )
            url = resolved.get("url", "")
            if url and not is_url_cached(url, self._media_cache_dir):
                self.prefetch_requested.emit(url)

    # ── Internal download helper ───────────────────────────────────────────────

    def _download(self, pub: str, lang: str, issue: str,
                  url: str, key: str, pub_ui: str,
                  emit_error: bool = True) -> bool:
        """
        Baixa o arquivo, emitindo progress. Retorna True se sucesso.

        emit_error: quando False, uma falha de download NÃO emite o sinal de
        erro. Usado no caminho stale-while-revalidate: se já servimos uma cópia
        local e a revalidação em segundo plano falha (ex.: rede caiu no meio do
        download), mantemos o que já está na tela em vez de sobrescrever com erro.
        """
        dest = self._cache.jwpub_path(pub, lang, issue)
        try:
            with stream_get(url, timeout=60, headers={"User-Agent": _UA}) as resp:
                total  = int(resp.headers.get("Content-Length") or 0)
                done   = 0
                chunks = []
                last_pct = -1
                for chunk in resp.iter_bytes(256 * 1024):
                    chunks.append(chunk)
                    done += len(chunk)
                    pct = int(done / total * 100) if total else 0
                    if pct != last_pct:
                        last_pct = pct
                        self.progress.emit(key, pub_ui, pct)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"".join(chunks))
            # New .jwpub on disk — wipe the stale extract dir so the next
            # _ensure_extract() call unpacks the fresh content instead of
            # returning the old x_<issue> directory.
            self._cache.invalidate_extract(pub, lang, issue)
            return True
        except (HttpError, OSError, ValueError) as exc:
            if emit_error:
                self.error.emit(key, pub_ui, str(exc))
            else:
                log.warning("%s %s/%s: background re-download failed, keeping "
                            "served cache: %s", pub_ui, pub, issue, exc)
            return False

    def _ensure_extract(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        ep = self._cache.extract_dir(pub, lang, issue)
        if ep.exists() and any(ep.glob("*.db")):
            return ep
        return self._cache.extract(pub, lang, issue)


# ── JwpubService — vive na main thread, gerencia o worker thread ──────────────

class JwpubService(QObject):
    """
    API pública para a UI. Vive na main thread.
    Cria um _JwpubWorker num QThread dedicado e encaminha pedidos via sinais.

    Sinais para a UI:
      mwb_ready(key, WeekData)
      wt_ready(key, WeekData)
      cbs_ready(key, WeekData)
      week_ready(key, WeekData)
      progress(key, pub, pct)
      error_sig(key, pub, msg)
      video_resolved(request_id, url, title, thumbnail)
    """
    mwb_ready      = Signal(str, object)
    wt_ready       = Signal(str, object)
    cbs_ready      = Signal(str, object)
    week_ready     = Signal(str, object)
    progress       = Signal(str, str, int)
    error_sig      = Signal(str, str, str)
    video_resolved = Signal(str, str, str, str)   # request_id, url, title, thumb

    # Sinais internos para o worker (despacham para a worker thread)
    _sig_load_week         = Signal(object, bool)
    _sig_set_lang          = Signal(str)
    _sig_set_sign_language = Signal(bool)
    _sig_resolve           = Signal(str, str, int, int, int, str)
    _sig_prefetch_wd       = Signal(object)

    def __init__(
        self,
        media_settings: MediaSettingsStore,
        cache_manager: MediaCacheManager,
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_settings = media_settings
        self._cache_manager = cache_manager
        self._active: dict[str, WeekData] = {}
        self._lang   = "T"
        self._is_sign_language = False

        # Cria worker + thread dedicada
        self._thread = QThread(self)
        self._worker = _JwpubWorker(
            cache_manager.media_cache_dir,
            jwpub_cache_dir,
            checksum_store,
        )
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        # Worker → JwpubService (main thread, QueuedConnection automática)
        self._worker.mwb_done.connect(self._on_mwb_done)
        self._worker.wt_done.connect(self._on_wt_done)
        self._worker.cbs_done.connect(self._on_cbs_done)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.error.connect(self._on_worker_error)
        self._worker.video_resolved.connect(self.video_resolved)
        self._worker.prefetch_requested.connect(self._on_prefetch_requested)

        # JwpubService → Worker (worker thread, QueuedConnection automática)
        self._sig_load_week.connect(self._worker.load_week)
        self._sig_set_lang.connect(self._worker.set_lang)
        self._sig_set_sign_language.connect(self._worker.set_sign_language)
        self._sig_resolve.connect(self._worker.resolve_video_async)
        self._sig_prefetch_wd.connect(self._worker.prefetch_week_media)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread de reuniões."""
        thread: QThread | None = getattr(self, "_thread", None)
        if thread is None:
            return
        try:
            running = thread.isRunning()
        except RuntimeError:
            self._thread = None
            return

        if running:
            thread.quit()
            thread.wait(wait_ms)
            try:
                still_running = thread.isRunning()
            except RuntimeError:
                self._thread = None
                return
            if still_running and delete_when_stopped:
                self.setParent(None)
                thread.finished.connect(self.deleteLater)

    def __del__(self):
        try:
            self.shutdown(wait_ms=0)
        except Exception:  # noqa: BLE001 - destructors must never raise during interpreter shutdown
            log.debug("Failed to shutdown JwpubService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        self._lang = lang
        self._sig_set_lang.emit(lang)

    def set_sign_language(self, is_sign: bool) -> None:
        """
        Informa ao worker se o idioma de mídia é gestual.
        Quando True, cânticos com key_symbol='sjjm' são resolvidos como 'sjj'.
        Deve ser chamado sempre que o idioma de mídia mudar.
        """
        self._is_sign_language = bool(is_sign)
        self._sig_set_sign_language.emit(is_sign)

    def get_lang(self) -> str:
        return self._lang

    def is_sign_language(self) -> bool:
        return self._is_sign_language

    def load_week(self, monday: date, force: bool = False):
        key = monday.isoformat()
        if not force and key in self._active:
            # Já existe entrada — só recarrega se alguma metade da semana falhou.
            existing = self._active[key]
            if existing.mwb_status not in ("error",) and existing.wt_status not in ("error",):
                return
        wd = WeekData(monday=monday)
        self._active[key] = wd
        self._sig_load_week.emit(monday, force)

    def get_week_data(self, monday: date) -> Optional[WeekData]:
        return self._active.get(monday.isoformat())

    def resolve_video_async(self, request_id: str, item: "MeetingMedia"):
        """
        Resolve URL de vídeo de forma assíncrona.
        Resultado chega via signal video_resolved(request_id, url, title, thumb).
        NUNCA bloqueia a main thread.
        """
        self._sig_resolve.emit(
            request_id,
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
        )

    def resolve_video(self, item: "MeetingMedia") -> dict:
        """
        Resolve URL de vídeo de forma SÍNCRONA (bloqueia a main thread ~200ms).
        Use apenas para ações pontuais do usuário (ex: clique em reproduzir),
        nunca durante construção de widgets ou loops.
        Para uso não-bloqueante, prefira resolve_video_async().
        """
        return _resolve_video(
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
            is_sign_language=self._is_sign_language,
        )

    def clear_week(self, monday: date):
        self._active.pop(monday.isoformat(), None)

    def auto_download_if_enabled(self):
        if not self._media_settings.meetings_auto_download():
            return
        mon      = _monday_of_week(date.today())
        next_mon = mon + timedelta(weeks=1)

        # Conecta antes de disparar load_week — garante que não perdemos o sinal
        # caso o load seja muito rápido (cache quente).
        if not getattr(self, "_auto_dl_connected", False):
            self._auto_dl_connected = True
            self.mwb_ready.connect(self._on_auto_dl_ready)
            self.wt_ready.connect(self._on_auto_dl_ready)
            self.cbs_ready.connect(self._on_auto_dl_ready)

        # Se a semana atual já está pronta (cache quente), o mwb_ready já foi emitido
        # antes desta conexão — dispara o prefetch diretamente.
        mon_wd = self._active.get(mon.isoformat())
        if mon_wd and mon_wd.mwb_status == "ready":
            self._sig_prefetch_wd.emit(mon_wd)

        # A semana atual já foi carregada por _navigate_to — não recarregar.
        # Apenas carrega a semana seguinte (que ainda não foi pedida).
        self.load_week(next_mon)

    @Slot(str, object)
    def _on_auto_dl_ready(self, key: str, wd: object):
        if not self._media_settings.meetings_auto_download():
            return
        mon = _monday_of_week(date.today())
        target_keys = {mon.isoformat(), (mon + timedelta(weeks=1)).isoformat()}
        if key in target_keys:
            self._sig_prefetch_wd.emit(wd)

    # ── Worker callbacks (chegam na main thread via QueuedConnection) ─────────

    @Slot(str, object)
    def _on_mwb_done(self, key: str, wd: WeekData):
        existing = self._active.get(key)
        if existing:
            existing.mwb_pub_dir     = wd.mwb_pub_dir
            existing.mwb_cover_bytes = wd.mwb_cover_bytes
            existing.mwb_date_label  = wd.mwb_date_label
            existing.mwb_week_title  = wd.mwb_week_title
            existing.mwb_all_media   = wd.mwb_all_media
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.mwb_status      = wd.mwb_status
            existing.mwb_issue       = wd.mwb_issue
            existing.cbs_ref         = wd.cbs_ref
            existing.cbs_status      = wd.cbs_status
            self.mwb_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.mwb_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_wt_done(self, key: str, wd: WeekData):
        existing = self._active.get(key)
        if existing:
            existing.wt_pub_dir     = wd.wt_pub_dir
            existing.wt_cover_bytes = wd.wt_cover_bytes
            existing.wt_study_title = wd.wt_study_title
            existing.wt_issue       = wd.wt_issue
            existing.wt_all_media   = wd.wt_all_media
            existing.wt_status      = wd.wt_status
            self.wt_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.wt_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_cbs_done(self, key: str, wd: WeekData):
        existing = self._active.get(key)
        if existing:
            existing.cbs_pub_dir = wd.cbs_pub_dir
            existing.cbs_items   = wd.cbs_items
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.cbs_status  = wd.cbs_status
            self.cbs_ready.emit(key, existing)
        else:
            self._active[key] = wd
            self.cbs_ready.emit(key, wd)

    @Slot(str, str, int)
    def _on_worker_progress(self, key: str, pub: str, pct: int):
        self.progress.emit(key, pub, pct)

    @Slot(str, str, str)
    def _on_worker_error(self, key: str, pub: str, msg: str):
        # "NOT_FOUND" is a sentinel emitted by the worker when the JW API
        # confirmed the publication does not exist (empty files list).
        # Any other message means a generic connectivity / extraction failure.
        status = "not_found" if msg == "NOT_FOUND" else "error"
        wd = self._active.get(key)
        if wd:
            if pub == "mwb":
                wd.mwb_status = status
            elif pub == "wt":
                wd.wt_status = status
        self.error_sig.emit(key, pub, msg)

    @Slot(str)
    def _on_prefetch_requested(self, url: str):
        """Recebe pedido de prefetch do worker — já estamos na main thread."""
        mgr = self._cache_manager
        if not mgr.is_cached(url) and not mgr.is_prefetching(url):
            mgr.prefetch(url)

    def _check_complete(self, key: str):
        wd = self._active.get(key)
        if wd and wd.mwb_status in ("ready", "empty") \
                and wd.wt_status in ("ready", "empty"):
            self.week_ready.emit(key, wd)


# ── Public helpers ────────────────────────────────────────────────────────────

def current_monday() -> date:
    return _monday_of_week(date.today())
