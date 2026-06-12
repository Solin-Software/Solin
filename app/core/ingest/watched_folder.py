"""
watched_folder.py — Solin
===================================
Serviço de Pasta Monitorada.

Responsabilidades:
  - Escanear a pasta raiz configurada e retornar subpastas de nível 1 como playlists
  - Escanear uma subpasta e retornar seus arquivos de mídia como itens de playlist
  - Converter documentos (PDF/PPTX/DOCX) salvando JPEGs na cache da subpasta
  - Processar .jwpub (extrair imagens + resolver vídeos)
  - Processar .jwlplaylist (extrair embedded media + resolver URLs)
  - Observar alterações no filesystem via QFileSystemWatcher
  - Manter manifesto de arquivos processados para evitar re-processamento

Regras de escaneamento:
  - Apenas subpastas imediatas (1 nível) da raiz são playlists
  - Arquivos diretamente na raiz são ignorados
  - Arquivos em sub-subdiretórios são ignorados (exceto .solin_cache)
  - Formatos suportados como itens: VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS
  - .jwpub, .jwlplaylist, PDF, PPTX, DOCX são processados e seus outputs aparecem

Manifesto (_solin_manifest.json):
  - Vive dentro de cada subpasta
  - Rastreia arquivos processados com fingerprint (size + mtime)
  - Lista outputs gerados (imagens) e virtual items (vídeos de URL)

Cache (.solin_cache/):
  - Subpasta dentro de cada subpasta monitorada
  - Armazena outputs gerados (JPEGs de PDF, imagens de JWPUB, embedded de JWLPLAYLIST)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QObject, QFileSystemWatcher, Signal, QThread

from app.core.foundation.constants import (
    VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS, PDF_EXTS, PPTX_EXTS, DOCX_EXTS,
    JWPUB_EXTS, PLAYLIST_EXTS,
)

log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────

SCAN_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS
WATCHED_DOC_EXTS: frozenset[str] = PDF_EXTS | PPTX_EXTS | DOCX_EXTS
WATCHED_DOC_TYPES: frozenset[str] = frozenset(
    ext.lstrip(".")
    for ext in WATCHED_DOC_EXTS
)
PROCESSABLE_EXTS: frozenset[str] = PDF_EXTS | PPTX_EXTS | DOCX_EXTS | JWPUB_EXTS | PLAYLIST_EXTS
MEETING_FOLDER_SOURCE_EXTS: frozenset[str] = (
    VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | PDF_EXTS | PPTX_EXTS | DOCX_EXTS
    | JWPUB_EXTS | PLAYLIST_EXTS
)

MANIFEST_FILE = "_solin_manifest.json"


# ── Path portability helpers ────────────────────────────────────────────────────

def _to_manifest_url(url: str, subfolder: Path) -> str:
    """Convert an absolute URL to a portable relative path for JSON storage.

    If the file lives inside *subfolder* (root or .solin_cache), we store only
    the relative portion so the manifest works on any PC sharing the folder via
    cloud sync (Dropbox, Google Drive, OneDrive, etc.).

    Remote URLs (http/https) and paths outside the subfolder are returned
    unchanged.
    """
    if not url or url.startswith(("http://", "https://")):
        return url
    try:
        rel = Path(url).relative_to(subfolder)
        # Use forward-slash for cross-OS compat (Windows reads both separators)
        return rel.as_posix()
    except ValueError:
        # File is outside the subfolder (e.g. legacy absolute from another PC)
        return url


def _from_manifest_url(url: str, subfolder: Path) -> str:
    """Resolve a manifest URL back to an absolute path on this machine.

    Handles three cases:
      1. Already absolute and valid → return as-is (legacy local data)
      2. Relative path → join with *subfolder*
      3. Legacy absolute from another PC (path does not exist but contains
         the subfolder name) → extract the relative tail and re-root it
    """
    if not url or url.startswith(("http://", "https://")):
        return url

    p = Path(url)

    # Case 1: it's already an absolute path
    if p.is_absolute():
        if p.exists():
            return str(p)
        # Case 3: legacy absolute from another PC.
        # Since it's an internal watched folder item, its relative path
        # MUST be either just the filename, or .solin_cache/filename.
        # We don't need to heuristically search for the subfolder name;
        # we just look at the last two parts of the path.
        parts = p.parts
        if len(parts) >= 2 and parts[-2] == CACHE_DIR_NAME:
            tail = Path(parts[-2]) / parts[-1]
        else:
            tail = Path(parts[-1])
        
        candidate = subfolder / tail
        return str(candidate)

    # Case 2: relative path → resolve against subfolder
    return str(subfolder / p)
CACHE_DIR_NAME = ".solin_cache"

_DPI = 150
_PAGE_FMT = "{stem}-page_{n:03d}.jpg"
_PAGE_CACHE_VERSION = 1
_MANIFEST_LOCK = threading.RLock()


# ── Utilitários ────────────────────────────────────────────────────────────────

def _path_id(path: str | Path) -> str:
    """ID estável derivado do caminho absoluto normalizado (16 chars hex)."""
    return hashlib.md5(os.path.normpath(str(path)).encode()).hexdigest()[:16]


def _media_type(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "image"


def _meeting_folder_source_key(path: Path) -> str:
    """Stable per-machine key for a source file in a meeting-targeted folder."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _meeting_folder_file_signature(path: Path) -> dict:
    """Fast change signature used to decide whether an autoimport can be reused."""
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _meeting_folder_source_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in SCAN_EXTS:
        return "media"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in JWPUB_EXTS:
        return "jwpub"
    if ext in PLAYLIST_EXTS:
        return "jwlplaylist"
    if ext in (PPTX_EXTS | DOCX_EXTS):
        return "lo"
    return ""


def meeting_folder_source_needs_processing(source: dict, record: dict | None) -> bool:
    """
    Return True when a meeting-folder source should be imported.

    Matching ``processed`` or ``failed`` records suppress retries until the file
    changes.  This is what keeps a removed autoimported item from reappearing
    while the original file remains untouched in the meeting folder.
    """
    if not isinstance(record, dict):
        return True
    status = str(record.get("status") or "")
    if status not in {"processed", "failed"}:
        return True
    return record.get("signature") != source.get("signature")


def local_file_availability_signature(urls: Iterable[str]) -> tuple[tuple[str, bool], ...]:
    """Return a stable snapshot of local-file availability for transient UI state.

    Linked-folder playlists intentionally keep missing local files in their saved
    data so cloud-sync placeholders can be shown.  This signature lets UI
    controllers detect when only the on-disk availability changed, without
    persisting that machine-local state into shared manifests.
    """
    states: dict[str, bool] = {}
    for url in urls:
        if not url or url.startswith(("http://", "https://")):
            continue
        norm = os.path.normcase(os.path.normpath(os.path.abspath(url)))
        states[norm] = os.path.exists(url)
    return tuple(sorted(states.items()))


def _cache_dir(subfolder: Path) -> Path:
    """Return the .solin_cache directory inside a subfolder, creating it if needed."""
    d = subfolder / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Manifesto ──────────────────────────────────────────────────────────────────

def _load_manifest(subfolder: Path) -> dict:
    """Load manifest from subfolder, return empty dict on error."""
    mf = subfolder / MANIFEST_FILE
    if not mf.exists():
        return {"version": 1, "processed": {}}
    try:
        with open(mf, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("processed"), dict):
            data["processed"] = {}
        return data
    except Exception:
        log.warning("Manifest corrupted in %s — will re-process", subfolder)
        return {"version": 1, "processed": {}}


def _save_manifest(subfolder: Path, manifest: dict) -> bool:
    """Atomically save a manifest, preserving the previous file on failure."""
    mf = subfolder / MANIFEST_FILE
    temp_path: Path | None = None
    try:
        manifest.setdefault("version", 1)
        with _MANIFEST_LOCK:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=subfolder,
                prefix=f".{MANIFEST_FILE}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(manifest, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, mf)
        return True
    except Exception as exc:
        log.error("Cannot write manifest to %s: %s", mf, exc)
        return False
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _commit_processed_entry(
    subfolder: Path,
    source_name: str,
    entry: dict,
) -> dict | None:
    """Merge and persist one processed entry without overwriting newer fields."""
    with _MANIFEST_LOCK:
        manifest = _load_manifest(subfolder)
        processed = manifest.setdefault("processed", {})
        previous = processed.get(source_name)
        if previous and entry.get("type") in WATCHED_DOC_TYPES:
            replacements = dict(
                zip(
                    previous.get("outputs", []),
                    entry.get("outputs", []),
                    strict=False,
                )
            )
            for item in manifest.get("playlist", {}).get("items", []):
                url = item.get("url", "")
                if not url or url.startswith(("http://", "https://")):
                    continue
                separator_index = max(url.rfind("/"), url.rfind("\\"))
                basename = url[separator_index + 1:]
                replacement = replacements.get(basename)
                if replacement:
                    item["url"] = url[:separator_index + 1] + replacement
        processed[source_name] = entry
        if not _save_manifest(subfolder, manifest):
            raise OSError(f"Could not persist manifest entry for '{source_name}'")
        return previous


def _file_fingerprint(path: Path) -> dict:
    """Return {size, mtime} for change detection (fast, no hashing)."""
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime}


def _fingerprint_matches(entry: dict, fp: dict) -> bool:
    """Check if a manifest entry still matches the file on disk."""
    return entry.get("size") == fp["size"] and entry.get("mtime") == fp["mtime"]


# ── Escaneamento ───────────────────────────────────────────────────────────────

def scan_root(folder_path: str) -> list[dict]:
    """
    Escaneia a pasta raiz e retorna subpastas imediatas como playlists virtuais.
    Subpastas cujo nome segue o padrão de reunião (YYYY-MM-DD MW|WE) são
    excluídas — são tratadas pelo meeting auto-assignment.
    Returns: Lista de dicts: {id, name, path, item_count}
    """
    from app.core.meetings.folder_matcher import is_meeting_folder

    root = Path(folder_path)
    if not root.is_dir():
        return []
    result = []
    for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        if is_meeting_folder(sub.name):
            continue
        items = scan_subfolder(str(sub))
        result.append({
            "id":         _path_id(sub),
            "name":       sub.name,
            "path":       str(sub),
            "item_count": len(items),
        })
    return result


def scan_meeting_folders(folder_path: str) -> list[dict]:
    """
    Return subfolders that match the meeting naming convention
    (``YYYY-MM-DD MW|WE``).  Each result contains:

    - ``path``:        absolute path to the subfolder
    - ``name``:        the folder name
    - ``monday``:      ISO date string of the JW meeting week Monday
    - ``meeting_tag``: ``"MW"`` or ``"WE"``
    - ``items``:       list of media items (same format as scan_subfolder)
    """
    from app.core.meetings.folder_matcher import match_meeting_folder

    root = Path(folder_path)
    if not root.is_dir():
        return []
    result = []
    for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        match = match_meeting_folder(sub.name)
        if not match:
            continue
        items = scan_subfolder(str(sub))
        result.append({
            "path":        str(sub),
            "name":        sub.name,
            "monday":      match.monday.isoformat(),
            "meeting_tag": match.meeting_tag,
            "items":       items,
        })
    return result


def scan_meeting_folder_sources(folder_path: str) -> list[dict]:
    """
    Return direct source files from meeting-targeted subfolders.

    Unlike :func:`scan_meeting_folders`, this scanner never reads or writes the
    linked-folder manifest and never inspects ``.solin_cache``.  The controller
    keeps the normal meeting-folder linked semantics while using profile caches
    for processed outputs.
    """
    from app.core.meetings.folder_matcher import match_meeting_folder

    root = Path(folder_path)
    if not root.is_dir():
        return []

    result: list[dict] = []
    for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        match = match_meeting_folder(sub.name)
        if not match:
            continue

        sources: list[dict] = []
        for file_path in sorted(sub.iterdir(), key=lambda p: p.name.lower()):
            if not file_path.is_file():
                continue
            if file_path.name.startswith(".") or file_path.name.startswith("_solin"):
                continue
            ext = file_path.suffix.lower()
            if ext not in MEETING_FOLDER_SOURCE_EXTS:
                continue
            kind = _meeting_folder_source_kind(file_path)
            if not kind:
                continue
            source = {
                "source_key": _meeting_folder_source_key(file_path),
                "path": str(file_path),
                "name": file_path.name,
                "title": file_path.stem,
                "ext": ext,
                "kind": kind,
                "signature": _meeting_folder_file_signature(file_path),
            }
            if kind == "media":
                source["media_type"] = _media_type(file_path)
            sources.append(source)

        result.append({
            "path": str(sub),
            "name": sub.name,
            "monday": match.monday.isoformat(),
            "meeting_tag": match.meeting_tag,
            "sources": sources,
        })
    return result

def scan_subfolder(subfolder_path: str) -> list[dict]:
    """
    Escaneia uma subpasta e retorna todos os itens de mídia:
    1. Arquivos de mídia na raiz da subpasta
    2. Arquivos de mídia em .solin_cache/ (outputs de processamento)
    3. Virtual items do manifesto (vídeos de .jwpub/.jwlplaylist)
    Ordenados por título (case-insensitive).
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return []

    items: list[dict] = []

    manifest = _load_manifest(sub)

    # Coleta todos os arquivos permitidos no cache (gerados pelo Solin)
    allowed_cache_files = set()
    for _src_name, entry in manifest.get("processed", {}).items():
        for out_name in entry.get("outputs", []):
            allowed_cache_files.add(out_name)

    # 1. Arquivos de mídia na raiz da subpasta
    for f in sub.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith("_solin") or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        if ext in SCAN_EXTS:
            items.append({
                "id":    _path_id(f),
                "title": f.stem,
                "url":   str(f),
                "type":  _media_type(f),
            })

    # 2. Arquivos de mídia em .solin_cache/ (apenas os legítimos)
    cache = sub / CACHE_DIR_NAME
    if cache.is_dir():
        for f in cache.iterdir():
            if not f.is_file():
                continue
            if f.name not in allowed_cache_files:
                continue
            ext = f.suffix.lower()
            if ext in SCAN_EXTS:
                items.append({
                    "id":    _path_id(f),
                    "title": f.stem,
                    "url":   str(f),
                    "type":  _media_type(f),
                })

    # 3. Virtual items do manifesto (URLs de vídeos/áudios de .jwpub/.jwlplaylist)
    for src_name, entry in manifest.get("processed", {}).items():
        for vi in entry.get("virtual_items", []):
            vid = vi.get("id") or _path_id(src_name + vi.get("url", ""))
            items.append({
                "id":           vid,
                "title":        vi.get("title", src_name),
                "url":          vi.get("url", ""),
                "type":         vi.get("type", "video"),
                "key_symbol":   vi.get("key_symbol"),
                "track":        vi.get("track"),
                "issue_tag":    vi.get("issue_tag"),
                "doc_id":       vi.get("doc_id"),
                "meps_language": vi.get("meps_language", 0),
                "_virtual":     True,
                "_source":      src_name,
            })

    # Ordena por título
    items.sort(key=lambda it: it.get("title", "").lower())
    return items


def get_pending_files(subfolder_path: str) -> list[str]:
    """
    Return list of processable source files that haven't been processed yet
    (or whose fingerprint changed).
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return []
    manifest = _load_manifest(sub)
    processed = manifest.get("processed", {})
    pending = []
    for f in sub.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in PROCESSABLE_EXTS:
            continue
        entry = processed.get(f.name)
        if entry is None:
            pending.append(str(f))
        else:
            fp = _file_fingerprint(f)
            fingerprint_changed = not _fingerprint_matches(entry, fp)
            document_cache_invalid = (
                ext in WATCHED_DOC_EXTS
                and [
                    Path(page).name
                    for page in pages_already_exist(f, sub / CACHE_DIR_NAME)
                ]
                != entry.get("outputs", [])
            )
            if fingerprint_changed or document_cache_invalid:
                pending.append(str(f))
    return pending


def reconcile_manifest(subfolder_path: str) -> list[str]:
    """
    Remove manifest entries for source files that no longer exist on disk.
    Deletes orphaned output files from .solin_cache/.
    Returns list of removed source file names.

    NOTE: Playlist items are intentionally preserved even when their physical
    files are absent.  This supports cloud-sync scenarios (Dropbox, GDrive,
    OneDrive) where a file may not yet be available on the local machine.
    The UI layer handles the "missing" state visually without touching the
    manifest, so both PCs share the same canonical JSON.
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return []
    manifest = _load_manifest(sub)
    processed = manifest.get("processed", {})
    removed = []
    cache = sub / CACHE_DIR_NAME

    for src_name in list(processed.keys()):
        src_path = sub / src_name
        if src_path.exists():
            continue
        # Source file gone — clean up outputs
        entry = processed.pop(src_name)
        for out_name in entry.get("outputs", []):
            out_path = cache / out_name
            try:
                if out_path.exists():
                    os.remove(out_path)
            except OSError as exc:
                log.warning("Cannot remove orphan %s: %s", out_path, exc)
        removed.append(src_name)
        log.info("Reconciled: removed %s and %d outputs", src_name, len(entry.get("outputs", [])))

    # Playlist items are NOT removed here — missing files are shown as
    # "Offline / Syncing" in the UI so their position is preserved.

    if removed:
        _save_manifest(sub, manifest)
    return removed


def load_manifest_playlist(subfolder_path: str) -> dict:
    """
    Load a playlist dict from the manifest, reconciled with current files on disk.
    New files found on disk are appended; missing files are preserved in their
    saved position (the UI layer renders them as "Offline / Syncing").

    URLs stored as relative paths (or legacy absolute paths from another PC)
    are resolved to absolute paths on this machine before returning.

    Returns a standard playlist dict: {id, name, items, sections}.
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return {"id": "", "name": "", "items": []}

    reconcile_manifest(subfolder_path)

    # Current items from disk scan
    current_items = scan_subfolder(subfolder_path)
    current_by_url: dict[str, dict] = {}
    for ci in current_items:
        url = ci.get("url", "")
        if url:
            current_by_url[url] = ci

    # Saved playlist from manifest
    manifest = _load_manifest(sub)
    saved_pl = manifest.get("playlist", {})
    saved_items = saved_pl.get("items", [])

    # Resolve saved URLs to absolute paths on this machine
    for si in saved_items:
        raw_url = si.get("url", "")
        si["url"] = _from_manifest_url(raw_url, sub)

    saved_urls = {it.get("url", "") for it in saved_items if it.get("url")}
    
    # Reconcile: keep saved order for ALL items, even if file is missing.
    # Missing files are rendered as "Offline / Syncing" by the UI.
    reconciled = []
    for si in saved_items:
        url = si.get("url", "")
        if url in current_by_url:
            # File found via disk scan — keep saved metadata
            merged = dict(si)
            reconciled.append(merged)
        elif url.startswith(("http://", "https://")):
            # Virtual item (video URL) — always keep
            reconciled.append(si)
        else:
            # Physical file not found on disk — preserve position anyway.
            # It may be syncing via a cloud service or temporarily moved.
            reconciled.append(si)

    # Add new items not in saved order
    for ci in current_items:
        url = ci.get("url", "")
        if url and url not in saved_urls:
            reconciled.append(ci)

    return {
        "id":       _path_id(sub),
        "name":     sub.name,
        "items":    reconciled,
        "sections": saved_pl.get("sections", []),
        "markers":  saved_pl.get("markers", []),
    }


def save_manifest_playlist(subfolder_path: str, pl: dict) -> None:
    """Save the playlist dict (items + sections) to the manifest.

    All local URLs that live inside the subfolder are converted to portable
    relative paths before writing, so the JSON works on any machine.
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return

    # Deep-copy items and convert URLs to relative for portability
    portable_items = []
    for item in pl.get("items", []):
        pi = dict(item)
        pi["url"] = _to_manifest_url(pi.get("url", ""), sub)
        portable_items.append(pi)

    manifest = _load_manifest(sub)

    manifest["playlist"] = {
        "items":    portable_items,
        "sections": pl.get("sections", []),
        "markers":  pl.get("markers", []),
    }
    _save_manifest(sub, manifest)


def remove_item_from_manifest(subfolder_path: str, item: dict) -> bool:
    """
    Remove a single item from a watched folder's manifest and delete its
    physical file if it resides inside the watched folder (either in the root
    or inside `.solin_cache/`).
    
    If the item is a virtual item (e.g. from a `.jwlplaylist`), it is removed
    specifically from the `virtual_items` list of its source file, ensuring
    it doesn't reappear unless the user re-adds the source file.

    Returns True if something was actually cleaned up.
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return False

    item_url = item.get("url", "")
    url_path = Path(item_url) if item_url else None
    changed = False

    # 1. Delete the physical file if it lives inside the watched folder
    # This covers both cache outputs (e.g. .solin_cache/page_1.jpg) AND
    # root media files (e.g. video.mp4 copied into the folder).
    if url_path and url_path.is_file():
        try:
            norm_sub = os.path.normpath(str(sub))
            norm_url = os.path.normpath(str(url_path))
            if norm_url.startswith(norm_sub + os.sep):
                os.remove(str(url_path))
                changed = True
                log.info("Removed physical file from watched folder: %s", url_path.name)
        except OSError as exc:
            log.warning("Cannot remove physical file %s: %s", url_path, exc)

    # 2. Update manifest → remove the output from its "processed" entry
    manifest = _load_manifest(sub)
    processed = manifest.get("processed", {})
    
    # a) If it's a virtual item, remove it from its source's virtual_items list
    if item.get("_virtual") and item.get("_source"):
        src_name = item["_source"]
        if src_name in processed:
            entry = processed[src_name]
            v_items = entry.get("virtual_items", [])
            original_len = len(v_items)
            entry["virtual_items"] = [vi for vi in v_items if vi.get("url") != item_url]
            if len(entry["virtual_items"]) != original_len:
                changed = True

    # b) If it's a physical output file, remove it from all outputs lists
    elif url_path:
        target_basename = url_path.name
        for _src_name, entry in processed.items():
            outputs = entry.get("outputs", [])
            if target_basename in outputs:
                outputs.remove(target_basename)
                changed = True
                break

    # 3. Remove from manifest playlist items (just in case it's still there)
    pl_data = manifest.get("playlist", {})
    pl_items = pl_data.get("items", [])
    if pl_items and item_url:
        new_items = [it for it in pl_items if it.get("url", "") != item_url]
        if len(new_items) != len(pl_items):
            pl_data["items"] = new_items
            changed = True

    if changed:
        _save_manifest(sub, manifest)
    return changed


def _document_cache_key(doc_path: str | Path) -> str:
    path = Path(doc_path)
    safe_name = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in path.name
    ).strip("._")[:48]
    digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
    return f"{safe_name or 'document'}-{digest}"


def _page_cache_marker(doc_path: str | Path, dest_dir: str | Path) -> Path:
    return Path(dest_dir) / f".{_document_cache_key(doc_path)}.pages.json"


def _page_cache_signature(doc_path: str | Path) -> dict[str, int | str]:
    path = Path(doc_path)
    stat = path.stat()
    return {
        "source": path.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def pages_already_exist(doc_path: str | Path, dest_dir: str | Path) -> list[str]:
    """Return a complete page cache matching the current source document."""
    marker = _page_cache_marker(doc_path, dest_dir)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("version") != _PAGE_CACHE_VERSION:
            return []
        if data.get("source") != _page_cache_signature(doc_path):
            return []

        page_names = data.get("pages")
        if not isinstance(page_names, list) or not page_names:
            return []

        cache_key = _document_cache_key(doc_path)
        expected_names = [
            _PAGE_FMT.format(stem=cache_key, n=index)
            for index in range(1, len(page_names) + 1)
        ]
        if page_names != expected_names:
            return []

        pages = [Path(dest_dir) / name for name in page_names]
        if not all(page.is_file() for page in pages):
            return []
        return [str(page) for page in pages]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _render_document_pages(
    source_path: Path,
    pdf_path: Path,
    dest_dir: Path,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    before_publish: Callable[[], None] | None = None,
) -> list[str]:
    """Render and publish a complete page set without exposing partial output."""
    from app.core.rendering.pdf import render_pdf_pages_sync

    dest_dir.mkdir(parents=True, exist_ok=True)
    source_signature = _page_cache_signature(source_path)
    cache_key = _document_cache_key(source_path)

    with tempfile.TemporaryDirectory(
        prefix=f".{cache_key}-render-",
        dir=dest_dir,
        ignore_cleanup_errors=True,
    ) as tmp:
        staging_dir = Path(tmp)
        staged_paths = [
            Path(path)
            for path in render_pdf_pages_sync(
                pdf_path,
                staging_dir,
                dpi=_DPI,
                page_name_format=_PAGE_FMT,
                page_stem=cache_key,
                image_format="JPEG",
                quality=None,
                progress_cb=progress_cb,
            )
        ]

        if before_publish is not None:
            before_publish()
        if _page_cache_signature(source_path) != source_signature:
            raise RuntimeError(f"Source changed during conversion: '{source_path.name}'")
        if not staged_paths:
            raise RuntimeError(f"Renderer produced no pages for '{source_path.name}'")

        expected_names = [
            _PAGE_FMT.format(stem=cache_key, n=index)
            for index in range(1, len(staged_paths) + 1)
        ]
        staged_set_is_valid = all(
            path.parent == staging_dir and path.is_file()
            for path in staged_paths
        )
        if [path.name for path in staged_paths] != expected_names or not staged_set_is_valid:
            raise RuntimeError(
                f"Renderer produced an invalid page set for '{source_path.name}'"
            )

        marker = _page_cache_marker(source_path, dest_dir)
        marker_payload = {
            "version": _PAGE_CACHE_VERSION,
            "source": source_signature,
            "pages": expected_names,
        }
        staged_marker = staging_dir / marker.name
        staged_marker.write_text(
            json.dumps(marker_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        marker.unlink(missing_ok=True)
        final_paths = [dest_dir / name for name in expected_names]
        for staged_path, final_path in zip(staged_paths, final_paths, strict=True):
            os.replace(staged_path, final_path)

        expected_set = set(expected_names)
        for stale_page in dest_dir.glob(f"{cache_key}-page_*.jpg"):
            if stale_page.name not in expected_set:
                stale_page.unlink(missing_ok=True)

        os.replace(staged_marker, marker)
        return [str(path) for path in final_paths]


def _publish_staged_outputs(
    staging_dir: Path,
    dest_dir: Path,
    output_names: list[str],
) -> list[str]:
    """Publish newly generated files and roll back if any move fails."""
    published: list[Path] = []
    try:
        for output_name in output_names:
            if Path(output_name).name != output_name:
                raise RuntimeError(f"Invalid staged output name: '{output_name}'")
            staged_path = staging_dir / output_name
            if not staged_path.is_file():
                raise RuntimeError(f"Missing staged output: '{output_name}'")
            final_path = dest_dir / output_name
            if final_path.exists():
                raise RuntimeError(f"Cache output already exists: '{output_name}'")
            os.replace(staged_path, final_path)
            published.append(final_path)
        return [str(path) for path in published]
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise


# ── Thread de conversão de documentos ─────────────────────────────────────────

class WatchedFolderDocConverter(QThread):
    """
    Converte PDF/PPTX/DOCX em imagens JPEG e salva na pasta de destino.
    Page names use a stable source-specific cache key to avoid collisions.

    Sinais:
        progress(current, total)     — progresso de página
        pages_ready(paths, stem)     — conversão concluída
        conversion_failed(error)     — erro
    """
    progress          = Signal(int, int)   # (current, total)
    pages_ready       = Signal(list, str)  # (jpeg_paths, stem)
    conversion_failed = Signal(str)        # mensagem de erro

    def __init__(self, doc_path: str, dest_dir: str, parent=None):
        super().__init__(parent)
        self._doc_path = doc_path
        self._dest_dir = dest_dir

    def run(self) -> None:
        doc_path = Path(self._doc_path)
        dest_dir = Path(self._dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = doc_path.stem

        # Verifica se já foi convertido anteriormente
        existing = pages_already_exist(doc_path, dest_dir)
        if existing:
            self.pages_ready.emit(existing, stem)
            return

        ext = doc_path.suffix.lower()
        try:
            if ext in PDF_EXTS:
                paths = self._convert_pdf(doc_path, dest_dir)
            elif ext in (PPTX_EXTS | DOCX_EXTS):
                paths = self._convert_lo(doc_path, dest_dir)
            else:
                self.conversion_failed.emit(f"Formato não suportado: {ext}")
                return
            self.pages_ready.emit(paths, stem)
        except ImportError as exc:
            self.conversion_failed.emit(str(exc))
        except RuntimeError as exc:
            self.conversion_failed.emit(str(exc))
        except Exception as exc:
            self.conversion_failed.emit(f"Erro inesperado: {exc}")

    def _convert_pdf(self, pdf_path: Path, dest_dir: Path) -> list[str]:
        return _render_document_pages(
            pdf_path,
            pdf_path,
            dest_dir,
            progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
        )

    def _convert_lo(self, lo_path: Path, dest_dir: Path) -> list[str]:
        from app.core.rendering.libreoffice import libreoffice_path

        soffice = libreoffice_path()
        if not soffice:
            raise RuntimeError(
                "LibreOffice não encontrado. Instale para converter "
                "apresentações e documentos (.pptx, .docx, .ppt, etc.)."
            )

        with tempfile.TemporaryDirectory(
            prefix="solin_wf_", ignore_cleanup_errors=True
        ) as tmp:
            tmp_path       = Path(tmp)
            lo_profile_uri = (tmp_path / "lo_profile").as_uri()

            result = subprocess.run(
                [
                    soffice, "--headless", "--norestore", "--nolockcheck",
                    "--nofirststartwizard",
                    f"-env:UserInstallation={lo_profile_uri}",
                    "--convert-to", "pdf", "--outdir", tmp, str(lo_path),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"LibreOffice falhou ao converter '{lo_path.name}':\n"
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )

            pdf_out = tmp_path / (lo_path.stem + ".pdf")
            if not pdf_out.exists():
                pdfs = list(tmp_path.glob("*.pdf"))
                if not pdfs:
                    raise RuntimeError(f"LibreOffice não gerou PDF para '{lo_path.name}'.")
                pdf_out = pdfs[0]

            return _render_document_pages(
                lo_path,
                pdf_out,
                dest_dir,
                progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
            )

# ── Thread de sincronização (processa arquivos pendentes) ─────────────────────

class WatchedFolderSyncThread(QThread):
    """
    Processa todos os arquivos pendentes (PDF/JWPUB/JWLPLAYLIST/PPTX/DOCX)
    em uma subpasta monitorada. Atualiza o manifesto ao final.

    Sinais:
        progress(filename, message)  — progresso por arquivo
        sync_complete()              — todos os arquivos processados
        sync_failed(error)           — erro fatal
    """
    progress      = Signal(str, str)    # (filename, message)
    sync_complete = Signal()
    sync_failed   = Signal(str)

    def __init__(
        self,
        subfolder_path: str,
        *,
        media_lang: str,
        fallback_lang_code: str,
        parent=None,
    ):
        super().__init__(parent)
        self._subfolder = subfolder_path
        self._media_lang = media_lang or "E"
        self._fallback_lang = fallback_lang_code or "E"

    def run(self) -> None:
        sub = Path(self._subfolder)
        if not sub.is_dir():
            self.sync_failed.emit(f"Folder not found: {sub}")
            return

        try:
            self._check_interrupted()
            # Reconcile first (remove orphans)
            reconcile_manifest(self._subfolder)
            self._check_interrupted()

            pending = get_pending_files(self._subfolder)
            if not pending:
                self.sync_complete.emit()
                return

            cache = _cache_dir(sub)

            for file_path in pending:
                self._check_interrupted()
                fp = Path(file_path)
                ext = fp.suffix.lower()
                self.progress.emit(fp.name, f"Processing {fp.name}…")
                outputs: list[str] = []
                committed = False

                try:
                    outputs, virtuals = self._process_file(fp, cache, ext)
                    fingerprint = _file_fingerprint(fp)
                    previous = _commit_processed_entry(sub, fp.name, {
                        "type": ext.lstrip("."),
                        "size": fingerprint["size"],
                        "mtime": fingerprint["mtime"],
                        "outputs": [os.path.basename(o) for o in outputs],
                        "virtual_items": virtuals,
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    committed = True
                    self._remove_replaced_outputs(cache, fp, previous, outputs)
                except InterruptedError:
                    raise
                except Exception as exc:
                    if outputs and not committed:
                        self._remove_uncommitted_outputs(cache, fp, outputs)
                    log.error("Sync failed for %s: %s", fp.name, exc)
                    self.progress.emit(fp.name, f"⚠ Error: {str(exc)[:60]}")

            self._check_interrupted()
            self.sync_complete.emit()

        except InterruptedError:
            log.info("Watched-folder sync cancelled for %s", self._subfolder)
        except Exception as exc:
            log.error("Sync thread error: %s", exc)
            self.sync_failed.emit(str(exc))

    def _process_file(self, fp: Path, cache: Path,
                      ext: str) -> tuple[list[str], list[dict]]:
        """Process a single file. Returns (output_paths, virtual_items)."""
        self._check_interrupted()
        if ext in PDF_EXTS:
            return self._process_pdf(fp, cache)
        elif ext in (PPTX_EXTS | DOCX_EXTS):
            return self._process_lo(fp, cache)
        elif ext in JWPUB_EXTS:
            return self._process_jwpub(fp, cache)
        elif ext in PLAYLIST_EXTS:
            return self._process_jwlplaylist(fp, cache)
        return [], []

    def _process_pdf(self, pdf_path: Path, cache: Path) -> tuple[list[str], list[dict]]:
        existing = pages_already_exist(pdf_path, cache)
        if existing:
            return existing, []
        paths = _render_document_pages(
            pdf_path,
            pdf_path,
            cache,
            progress_cb=lambda cur, tot: self._emit_render_progress(
                pdf_path.name,
                cur,
                tot,
            ),
            before_publish=self._check_interrupted,
        )
        return paths, []

    def _process_lo(self, lo_path: Path, cache: Path) -> tuple[list[str], list[dict]]:
        from app.core.rendering.libreoffice import libreoffice_path

        soffice = libreoffice_path()
        if not soffice:
            raise RuntimeError("LibreOffice not found")

        existing = pages_already_exist(lo_path, cache)
        if existing:
            return existing, []

        with tempfile.TemporaryDirectory(prefix="solin_wf_", ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            lo_profile_uri = (tmp_path / "lo_profile").as_uri()
            result = self._run_libreoffice(
                [soffice, "--headless", "--norestore", "--nolockcheck",
                 "--nofirststartwizard",
                 f"-env:UserInstallation={lo_profile_uri}",
                 "--convert-to", "pdf", "--outdir", tmp, str(lo_path)],
            )
            if result.returncode != 0:
                raise RuntimeError(f"LibreOffice failed: {result.stderr.strip()[:100]}")

            pdf_out = tmp_path / (lo_path.stem + ".pdf")
            if not pdf_out.exists():
                pdfs = list(tmp_path.glob("*.pdf"))
                if not pdfs:
                    raise RuntimeError(f"LibreOffice produced no PDF for '{lo_path.name}'")
                pdf_out = pdfs[0]

            paths = _render_document_pages(
                lo_path,
                pdf_out,
                cache,
                progress_cb=lambda cur, tot: self._emit_render_progress(
                    lo_path.name,
                    cur,
                    tot,
                ),
                before_publish=self._check_interrupted,
            )
            return paths, []

    def _process_jwpub(self, jwpub_path: Path, cache: Path) -> tuple[list[str], list[dict]]:
        self._check_interrupted()
        from app.core.jw.publication_reader import read_jwpub_for_playlist

        with tempfile.TemporaryDirectory(
            prefix=".jwpub-render-",
            dir=cache,
            ignore_cleanup_errors=True,
        ) as tmp:
            staging_dir = Path(tmp)
            items, stem = read_jwpub_for_playlist(
                str(jwpub_path),
                lang=self._media_lang,
                dest_images_dir=str(staging_dir),
                resolve_urls=True,
            )
            outputs = []
            virtuals = []
            for item in items:
                self._check_interrupted()
                if item.get("type") == "image" and item.get("url"):
                    image_path = Path(item["url"])
                    if image_path.parent != staging_dir or not image_path.is_file():
                        raise RuntimeError(
                            f"JWPUB image was not staged correctly: '{image_path.name}'"
                        )
                    outputs.append(image_path.name)
                else:
                    virtuals.append({
                        "id":            str(uuid.uuid4()),
                        "title":         item.get("title", stem),
                        "url":           item.get("url", ""),
                        "type":          item.get("type", "video"),
                        "key_symbol":    item.get("key_symbol"),
                        "track":         item.get("track"),
                        "issue_tag":     item.get("issue_tag"),
                        "doc_id":        item.get("doc_id"),
                        "meps_language": item.get("meps_language", 0),
                    })
            self._check_interrupted()
            return _publish_staged_outputs(staging_dir, cache, outputs), virtuals

    def _process_jwlplaylist(self, jwl_path: Path, cache: Path) -> tuple[list[str], list[dict]]:
        self._check_interrupted()
        from app.core.playlists.reader import read_jwlplaylist
        data = read_jwlplaylist(str(jwl_path), fallback_lang_code=self._fallback_lang)

        with tempfile.TemporaryDirectory(
            prefix=".jwlplaylist-render-",
            dir=cache,
            ignore_cleanup_errors=True,
        ) as tmp:
            staging_dir = Path(tmp)
            outputs = []
            virtuals = []
            for raw in data.get("items", []):
                self._check_interrupted()
                url = raw.get("url") or raw.get("jworg_url") or ""
                if raw.get("data") and not url:
                    ext = Path(raw.get("filename", "media")).suffix or ".mp4"
                    fname = f"embedded_{uuid.uuid4().hex[:12]}{ext}"
                    (staging_dir / fname).write_bytes(raw["data"])
                    outputs.append(fname)
                else:
                    virtuals.append({
                        "id":            str(uuid.uuid4()),
                        "title":         raw.get("title", jwl_path.stem),
                        "url":           url,
                        "type":          raw.get("type", "video"),
                        "key_symbol":    raw.get("key_symbol"),
                        "track":         raw.get("track"),
                        "issue_tag":     raw.get("issue_tag"),
                        "doc_id":        raw.get("doc_id"),
                        "meps_language": raw.get("language", 0),
                    })
            self._check_interrupted()
            return _publish_staged_outputs(staging_dir, cache, outputs), virtuals

    @staticmethod
    def _remove_replaced_outputs(
        cache: Path,
        source_path: Path,
        previous: dict | None,
        current_outputs: list[str],
    ) -> None:
        if previous:
            current_names = {Path(path).name for path in current_outputs}
            for old_name in previous.get("outputs", []):
                if old_name not in current_names:
                    try:
                        (cache / old_name).unlink(missing_ok=True)
                    except OSError:
                        log.warning(
                            "Cannot remove replaced cache output %s",
                            cache / old_name,
                            exc_info=True,
                        )

        if source_path.suffix.lower() in WATCHED_DOC_EXTS:
            legacy_marker = cache / f".{source_path.stem}.pages.json"
            if legacy_marker != _page_cache_marker(source_path, cache):
                try:
                    legacy_marker.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "Cannot remove legacy page marker %s",
                        legacy_marker,
                        exc_info=True,
                    )

    @staticmethod
    def _remove_uncommitted_outputs(
        cache: Path,
        source_path: Path,
        outputs: list[str],
    ) -> None:
        manifest = _load_manifest(cache.parent)
        referenced = {
            output_name
            for entry in manifest.get("processed", {}).values()
            for output_name in entry.get("outputs", [])
        }
        for output in outputs:
            output_path = cache / Path(output).name
            if output_path.name in referenced:
                continue
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Cannot roll back uncommitted cache output %s",
                    output_path,
                    exc_info=True,
                )

        if source_path.suffix.lower() in WATCHED_DOC_EXTS:
            try:
                _page_cache_marker(source_path, cache).unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Cannot roll back page cache marker for %s",
                    source_path,
                    exc_info=True,
                )

    def _check_interrupted(self) -> None:
        if self.isInterruptionRequested():
            raise InterruptedError("Watched-folder sync cancelled")

    def _emit_render_progress(self, filename: str, current: int, total: int) -> None:
        self._check_interrupted()
        self.progress.emit(filename, f"Page {current}/{total}")

    def _run_libreoffice(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 120
        while process.poll() is None:
            if self.isInterruptionRequested():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                raise InterruptedError("LibreOffice conversion cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                raise RuntimeError(
                    "LibreOffice timed out: "
                    + (stderr.strip() or stdout.strip())[:100]
                )
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(
            args=args,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


# ── Observador de pasta ────────────────────────────────────────────────────────

class WatchedFolderWatcher(QObject):
    """
    Observa a pasta raiz monitorada e seus subdiretórios imediatos.
    Emite `changed` quando qualquer mudança de arquivo ou pasta é detectada.
    Emite `subfolder_changed(path)` quando uma subpasta específica muda.
    """
    changed           = Signal()        # qualquer mudança
    subfolder_changed = Signal(str)     # subpasta específica

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher   = QFileSystemWatcher(self)
        self._root_path = ""
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._watcher.fileChanged.connect(self._on_file_changed)

    def set_root(self, path: str) -> None:
        """Define ou troca a pasta raiz sendo observada."""
        old = self._watcher.directories() + self._watcher.files()
        if old:
            self._watcher.removePaths(old)

        self._root_path = path
        if not path or not Path(path).is_dir():
            return

        # Observa raiz + cada subpasta imediata + .solin_cache de cada
        paths_to_watch = [path]
        for sub in Path(path).iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                paths_to_watch.append(str(sub))
                cache_sub = sub / CACHE_DIR_NAME
                if cache_sub.is_dir():
                    paths_to_watch.append(str(cache_sub))
        self._watcher.addPaths(paths_to_watch)

    def watch_subfolder(self, path: str) -> None:
        """Adiciona uma subpasta específica ao watcher (caso ainda não esteja)."""
        if path and Path(path).is_dir():
            self._watcher.addPath(path)
            # Also watch its cache dir
            cache = Path(path) / CACHE_DIR_NAME
            if cache.is_dir():
                self._watcher.addPath(str(cache))

    def _on_dir_changed(self, path: str) -> None:
        if path == self._root_path:
            self.set_root(self._root_path)
        else:
            # If it's a .solin_cache change, emit the parent subfolder
            p = Path(path)
            if p.name == CACHE_DIR_NAME:
                self.subfolder_changed.emit(str(p.parent))
            else:
                # Se a subpasta mudou, pode ser que o .solin_cache tenha sido
                # recém-criado pelo sincronismo do Dropbox ou conversão local.
                # Garantimos que ele passa a ser observado instantaneamente:
                cache = p / CACHE_DIR_NAME
                if cache.is_dir():
                    self._watcher.addPath(str(cache))
                
                self.subfolder_changed.emit(path)
        self.changed.emit()

    def _on_file_changed(self, path: str) -> None:
        parent = Path(path).parent
        # If file is inside .solin_cache, emit the grandparent
        if parent.name == CACHE_DIR_NAME:
            self.subfolder_changed.emit(str(parent.parent))
        else:
            self.subfolder_changed.emit(str(parent))
        self.changed.emit()
