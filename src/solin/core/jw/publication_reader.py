"""
publication_reader.py — Solin
=======================
Centralized .jwpub file reader for playlist and wifi use cases.

Provides two APIs:

1. Synchronous (for direct use in background threads):
    read_jwpub_for_playlist(jwpub_path, lang, dest_images_dir)
        → (list[item_dict], stem)

2. Async QThread (preferred — signals work reliably in main thread):
    thread = JwpubImportThread(jwpub_path, lang, dest_images_dir, parent)
    thread.items_ready.connect(my_slot)   # (list[dict], str stem)
    thread.failed.connect(my_error_slot)  # (str error)
    thread.start()

Each item_dict is playlist-compatible:
    {
      "title":       str,
      "url":         str,   # absolute local path (images) or CDN URL (videos)
      "type":        "image" | "video" | "audio",
      "key_symbol":  str | None,
      "track":       int | None,
      "issue_tag":   int | None,
      "doc_id":      int | None,
      "meps_language": 0,
    }

Rules:
- Images: FilePath from zip → copied to dest_images_dir (persistent).
- Videos/audio: resolved via JW API. issue_tag=0 kept as 0 during resolution
  (NOT coerced to None), stored as None in playlist JSON since 0 = "no issue".
- CategoryType 9, 10, 15, 25 (covers / nav thumbnails) always skipped.
- Duplicate MultimediaIds within the same file are deduplicated.

This module has NO Qt dependencies in the sync function — safe for threads.
The QThread class is only imported when JwpubImportThread is used.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# CategoryTypes excluded from all JW pub media queries
_EXCL_CAT = frozenset({9, 10, 15, 25})

# ── Synchronous API (call from background thread) ─────────────────────────────

def read_jwpub_for_playlist(
    jwpub_path: str,
    lang: str = "T",
    dest_images_dir: Optional[str] = None,
    resolve_urls: bool = True,
) -> tuple[list[dict], str]:
    """
    Parse a .jwpub file and return a list of playlist-compatible item dicts.

    Parameters
    ----------
    jwpub_path : str
        Absolute path to the .jwpub file.
    lang : str
        JW API language code (e.g. "T" for Portuguese, "E" for English).
    dest_images_dir : str | None
        Directory where extracted images should be copied for persistence.
        If None, uses a system tempdir (images will be lost on reboot).
    resolve_urls : bool
        If True (default), resolve video/audio CDN URLs via JW API synchronously.
        Set False when you want raw items and will resolve later.

    Returns
    -------
    (items, stem)
        items : list of playlist-compatible dicts
        stem  : filename stem (e.g. "mwb_T_202603") for display purposes
    """
    stem = Path(jwpub_path).stem
    temporary_dir: tempfile.TemporaryDirectory | None = None

    # ── Extract inner zip ──────────────────────────────────────────────────
    try:
        with zipfile.ZipFile(jwpub_path, "r") as outer:
            if "contents" not in outer.namelist():
                raise ValueError("Not a valid .jwpub (no 'contents' entry)")
            inner_bytes = outer.read("contents")
        with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner:
            db_names = [n for n in inner.namelist() if n.endswith(".db")]
            if not db_names:
                raise ValueError("No SQLite database found inside .jwpub")
            if dest_images_dir:
                temporary_dir = tempfile.TemporaryDirectory(
                    prefix="solin_jwpub_",
                    ignore_cleanup_errors=True,
                )
                tmp_dir = temporary_dir.name
            else:
                tmp_dir = tempfile.mkdtemp(prefix="solin_jwpub_")
            inner.extractall(tmp_dir)
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        log.error("jwpub_reader: cannot extract %s: %s", jwpub_path, exc)
        raise

    # ── Read database ──────────────────────────────────────────────────────
    db_path = os.path.join(tmp_dir, os.path.basename(db_names[0]))
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT DISTINCT
                   m.MultimediaId,
                   m.FilePath,
                   m.MimeType,
                   m.CategoryType,
                   m.Label,
                   m.Caption,
                   m.KeySymbol,
                   m.Track,
                   m.IssueTagNumber,
                   m.MepsDocumentId
            FROM   Multimedia m
            WHERE  m.CategoryType NOT IN (9, 10, 15, 25)
              AND  (
                     (m.FilePath IS NOT NULL AND m.FilePath != '')
                     OR m.KeySymbol IS NOT NULL
                     OR m.MepsDocumentId IS NOT NULL
                   )
            ORDER  BY m.MultimediaId
        """).fetchall()
    except sqlite3.Error as exc:
        log.error("jwpub_reader: cannot read db %s: %s", db_path, exc)
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                log.debug("jwpub_reader: could not close db %s", db_path, exc_info=True)

    # ── Persistent image destination ────────────────────────────────────────
    if dest_images_dir:
        os.makedirs(dest_images_dir, exist_ok=True)
        img_dest = dest_images_dir
    else:
        img_dest = tmp_dir   # temporary — caller is responsible

    # ── Build items ─────────────────────────────────────────────────────────
    image_items: list[dict] = []
    video_rows:  list[dict] = []

    seen_ids: set[int] = set()
    for r in rows:
        mid = r["MultimediaId"]
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        mime  = (r["MimeType"] or "").lower()
        fp    = r["FilePath"] or ""
        label = (r["Label"] or r["Caption"] or "").strip()

        if mime.startswith("image") and fp:
            src = os.path.join(tmp_dir, fp)
            if not os.path.exists(src):
                continue
            if img_dest != tmp_dir:
                # Copy to persistent location with a unique name
                ext      = Path(fp).suffix or ".jpg"
                dst_name = f"{uuid.uuid4().hex}{ext}"
                dst      = os.path.join(img_dest, dst_name)
                shutil.copy2(src, dst)
            else:
                dst = src

            image_items.append({
                "title":         label or Path(fp).stem,
                "url":           dst,
                "type":          "image",
                "key_symbol":    None,
                "track":         None,
                "issue_tag":     None,
                "doc_id":        None,
                "meps_language": 0,
            })

        elif mime.startswith("video") or mime.startswith("audio"):
            sym   = r["KeySymbol"] or ""
            track = r["Track"]
            # CRITICAL: keep issue as int even when 0
            issue = r["IssueTagNumber"]
            issue = 0 if issue is None else int(issue)
            meps  = r["MepsDocumentId"] or 0
            mtype = "audio" if mime.startswith("audio") else "video"

            if not sym and not meps:
                continue   # nothing to resolve

            video_rows.append({
                "sym":   sym,
                "track": track if track is not None else 0,
                "issue": issue,
                "meps":  meps,
                "mtype": mtype,
                "label": label,
            })

    # ── Resolve video URLs ──────────────────────────────────────────────────
    video_items: list[dict] = []
    if resolve_urls and video_rows:
        from solin.core.meetings.publications import _resolve_video
        for vr in video_rows:
            try:
                api   = _resolve_video(vr["sym"], vr["track"], vr["issue"],
                                       vr["meps"], lang)
                url   = api.get("url", "")
                title = api.get("title", "") or vr["label"] or vr["sym"] or stem
            except Exception:  # noqa: BLE001 - per-item external resolver isolation
                log.debug(
                    "jwpub_reader: resolve failed sym=%s",
                    vr["sym"],
                    exc_info=True,
                )
                url   = ""
                title = vr["label"] or vr["sym"] or stem

            video_items.append({
                "title":         title,
                "url":           url,
                "type":          vr["mtype"],
                "key_symbol":    vr["sym"] or None,
                "track":         vr["track"] if vr["track"] else None,
                # issue_tag: keep 0 as None in playlist JSON (0 = "no issue tag")
                # but key_symbol+track allow re-resolution if needed
                "issue_tag":     vr["issue"] if vr["issue"] != 0 else None,
                "doc_id":        vr["meps"] if vr["meps"] else None,
                "meps_language": 0,
            })
    elif video_rows:
        for vr in video_rows:
            video_items.append({
                "title":         vr["label"] or vr["sym"] or stem,
                "url":           "",
                "type":          vr["mtype"],
                "key_symbol":    vr["sym"] or None,
                "track":         vr["track"] if vr["track"] else None,
                "issue_tag":     vr["issue"] if vr["issue"] != 0 else None,
                "doc_id":        vr["meps"] if vr["meps"] else None,
                "meps_language": 0,
            })

    result = image_items + video_items, stem
    if temporary_dir is not None:
        temporary_dir.cleanup()
    return result


# ── Async QThread API (use this from Qt widgets) ───────────────────────────────

class JwpubImportThread:
    """
    Factory that returns a QThread subclass for importing a .jwpub file.

    Usage:
        thread = JwpubImportThread.create(jwpub_path, lang, dest_dir, parent)
        thread.items_ready.connect(my_slot)   # (list[dict], str stem)
        thread.failed.connect(my_error_slot)  # (str error_msg)
        thread.start()

    Using a factory function avoids importing PySide6 at module load time.
    """

    @staticmethod
    def create(jwpub_path: str, lang: str = "T",
               dest_images_dir: Optional[str] = None,
               parent=None):
        """Create and return a ready-to-start QThread instance."""
        from PySide6.QtCore import QThread, Signal

        class _Thread(QThread):
            items_ready = Signal(list, str)   # (items, stem)
            failed      = Signal(str)         # error_msg

            def __init__(self, path, lang, dest, parent):
                super().__init__(parent)
                self._path = path
                self._lang = lang
                self._dest = dest

            def run(self):
                try:
                    items, stem = read_jwpub_for_playlist(
                        self._path,
                        lang=self._lang,
                        dest_images_dir=self._dest,
                        resolve_urls=True,
                    )
                    self.items_ready.emit(items, stem)
                except Exception as exc:  # noqa: BLE001 - QThread error-delivery boundary
                    log.exception("JWPUB import worker failed")
                    self.failed.emit(str(exc))

        return _Thread(jwpub_path, lang, dest_images_dir, parent)
