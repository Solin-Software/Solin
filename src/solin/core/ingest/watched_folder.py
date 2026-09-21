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
  - Registrar importações e organização no journal compartilhado

Regras de escaneamento:
  - Apenas subpastas imediatas (1 nível) da raiz são playlists
  - Arquivos diretamente na raiz são ignorados
  - Arquivos em sub-subdiretórios são ignorados (exceto .solin_cache)
  - Formatos suportados como itens: VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS
  - .jwpub, .jwlplaylist, PDF, PPTX, DOCX são processados e seus outputs aparecem

Journal (.solin_sync/playlist/):
  - Operações imutáveis preservam edições concorrentes e exclusões
  - Rastreia arquivos processados com assinatura portável (size + sha256)
  - Lista outputs gerados e ocorrências virtuais (URLs ou mídia embutida)
  - O manifesto antigo é importado uma vez como baseline de migração

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
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QFileSystemWatcher, Signal, QThread

from solin.core.foundation.constants import (
    PDF_EXTS, PPTX_EXTS, DOCX_EXTS, JWPUB_EXTS, PLAYLIST_EXTS,
)
from solin.core.foundation.resource_keys import ResourceClaim
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.media.formats import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    media_type_from_path,
)
from solin.core.playlists.items import create_playlist_item
from solin.core.playlists.linked_folder import (
    STATE as LINKED_SYNC_STATE,
    playlist_sync,
    read_linked_manifest,
)
from solin.core.ingest.sync.discovery import RESOURCE, suppression_record
from solin.core.ingest.sync.materialization import portable_playlist_url
from solin.core.ingest.sync.resources import (
    content_signature, portable_resource_key, resource_identity_key,
)
from solin.core.ingest.staging import is_watched_folder_staging_path
from solin.core.ingest.manifest import (
    CACHE_DIR_NAME,
    ManifestWriteError,
    cache_dir as _cache_dir,
    from_manifest_url as _from_manifest_url,
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

_DPI = 150
_PAGE_FMT = "{stem}-page_{n:03d}.jpg"
_PAGE_CACHE_VERSION = 1


# ── Utilitários ────────────────────────────────────────────────────────────────

def _path_id(path: str | Path) -> str:
    """ID estável derivado do caminho absoluto normalizado (16 chars hex)."""
    return hashlib.md5(os.path.normpath(str(path)).encode()).hexdigest()[:16]


def _physical_playlist_item(path: Path) -> dict:
    """Build a discovered file through the canonical playlist-item factory."""

    item = dict(
        create_playlist_item(
            title=path.stem,
            url=str(path),
            type=_media_type(path),
        )
    )
    item["id"] = _path_id(path)
    return item


def _media_type(path: str | Path) -> str:
    return media_type_from_path(path, default="image")


def _commit_processed_entry(
    subfolder: Path,
    source_name: str,
    entry: dict,
    *,
    before_commit: Callable[[], None] | None = None,
) -> dict | None:
    """Merge and persist one processed entry without overwriting newer fields."""
    service = playlist_sync(subfolder)
    with service.lock:
        snapshot = service.read()
        previous = service.manifest(snapshot)["processed"].get(source_name)
        if before_commit is not None:
            before_commit()
        service.update_processed(snapshot, source_name, entry)
        return previous


def _file_fingerprint(path: Path) -> dict:
    """Portable content revision with a cached local filesystem invalidator."""
    return content_signature(path)


def _fingerprint_matches(entry: dict, fp: dict) -> bool:
    """Check if a manifest entry still matches the file on disk."""
    return entry.get("size") == fp["size"] and entry.get("sha256") == fp["sha256"]


def _path_is_inside(path: str | Path, folder: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except (OSError, ValueError):
        return False


def _playlist_cache_references(manifest: dict) -> set[str]:
    """Return cache filenames directly referenced by the saved playlist."""

    names: set[str] = set()
    for item in manifest.get("playlist", {}).get("items", []):
        url = str(item.get("url") or "")
        if not url or url.startswith(("http://", "https://")):
            continue
        path = Path(url)
        parts = path.parts
        if len(parts) >= 2 and parts[-2] == CACHE_DIR_NAME:
            names.add(parts[-1])
    return names


# ── Escaneamento ───────────────────────────────────────────────────────────────

def scan_root(folder_path: str) -> list[dict]:
    """
    Escaneia a pasta raiz e retorna subpastas imediatas como playlists virtuais.
    Subpastas cujo nome segue o padrão de reunião (YYYY-MM-DD MW|WE) são
    excluídas — são tratadas pelo meeting auto-assignment.
    Returns: Lista de dicts: {id, name, path, item_count}
    """
    from solin.core.meetings.folder_matcher import is_meeting_folder

    root = Path(folder_path)
    if not root.is_dir():
        return []
    result = []
    for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        if is_meeting_folder(sub.name):
            continue
        playlist = load_manifest_playlist(str(sub))
        result.append({
            "id":         _path_id(sub),
            "name":       sub.name,
            "path":       str(sub),
            "item_count": len(playlist["items"]),
        })
    return result


def scan_subfolder(subfolder_path: str) -> list[dict]:
    """
    Escaneia uma subpasta e retorna todos os itens de mídia:
    1. Arquivos de mídia na raiz da subpasta
    2. Arquivos de mídia em .solin_cache/ (outputs de processamento)
    3. Ocorrências virtuais do manifesto (.jwpub/.jwlplaylist)
    Ordenados por título (case-insensitive).
    """
    sub = Path(subfolder_path)
    if not sub.is_dir():
        return []

    return _scan_subfolder(sub, read_linked_manifest(sub))


def _scan_subfolder(sub: Path, manifest: dict) -> list[dict]:
    """Scan one validated subfolder using an already loaded manifest."""

    items: list[dict] = []

    # Coleta todos os arquivos permitidos no cache (gerados pelo Solin)
    allowed_cache_files = set()
    output_sources: dict[str, list[tuple[str, int]]] = {}
    for _src_name, entry in manifest.get("processed", {}).items():
        for output_index, out_name in enumerate(entry.get("outputs", [])):
            allowed_cache_files.add(out_name)
            output_sources.setdefault(out_name, []).append((_src_name, output_index))
    allowed_cache_files.update(_playlist_cache_references(manifest))
    virtual_local_resources = {
        _manifest_resource_key(str(vi.get("url") or ""), sub)
        for entry in manifest.get("processed", {}).values()
        for vi in entry.get("virtual_items", [])
        if vi.get("url") and not str(vi["url"]).startswith(("http://", "https://"))
    }

    # 1. Arquivos de mídia na raiz da subpasta
    for f in sub.iterdir():
        if is_watched_folder_staging_path(f):
            continue
        if not f.is_file():
            continue
        if f.name.startswith("_solin") or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        if ext in SCAN_EXTS:
            items.append(_physical_playlist_item(f))

    # 2. Arquivos de mídia em .solin_cache/ (apenas os legítimos)
    cache = sub / CACHE_DIR_NAME
    if cache.is_dir():
        for f in cache.iterdir():
            if not f.is_file():
                continue
            if f.name not in allowed_cache_files:
                continue
            ext = f.suffix.lower()
            if (
                ext in SCAN_EXTS
                and _manifest_resource_key(str(f), sub) not in virtual_local_resources
            ):
                item = _physical_playlist_item(f)
                sources = output_sources.get(f.name, [])
                if len(sources) == 1:
                    item["_source"], item["_source_output"] = sources[0]
                items.append(item)

    # 3. Manifest-backed occurrences (remote media and embedded JWL assets).
    for src_name, entry in manifest.get("processed", {}).items():
        for occurrence_index, vi in enumerate(entry.get("virtual_items", [])):
            vid = vi.get("id") or _path_id(
                f"{src_name}:{occurrence_index}:{vi.get('url', '')}"
            )
            virtual_item = {
                "id":           vid,
                "title":        vi.get("title", src_name),
                "url":          _from_manifest_url(str(vi.get("url") or ""), sub),
                "type":         vi.get("type", "video"),
                "key_symbol":   vi.get("key_symbol"),
                "track":        vi.get("track"),
                "issue_tag":    vi.get("issue_tag"),
                "doc_id":       vi.get("doc_id"),
                "meps_language": vi.get("meps_language", 0),
                "_virtual":     True,
                "_source":      src_name,
            }
            for field in (
                "start_trim_ticks",
                "end_trim_ticks",
                "base_duration_ticks",
                "accuracy",
                "end_action",
            ):
                if field in vi:
                    virtual_item[field] = vi[field]
            items.append(virtual_item)

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
    manifest = read_linked_manifest(sub)
    processed = manifest.get("processed", {})
    pending = []
    for f in sub.iterdir():
        if is_watched_folder_staging_path(f):
            continue
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


def load_manifest_playlist(subfolder_path: str) -> dict:
    """Project shared operations and adopt low-priority filesystem discoveries.

    Missing resources keep their organization. The local snapshot accompanying
    this view is the causal baseline for subsequent user edits.
    """
    sub = Path(subfolder_path)
    service = playlist_sync(sub, refresh_binding=True)
    with service.lock:
        # Replay durably accepted edits after an application restart.
        sub = service.folder
        for pending in service.pending_intents():
            try:
                save_manifest_playlist(subfolder_path, pending)
            except ManifestWriteError as exc:
                if not exc.retryable:
                    raise
        snapshot = service.read()
        manifest = service.manifest(snapshot)
        scanned = _scan_subfolder(sub, manifest) if sub.is_dir() else []
        snapshot = service.discover(snapshot, scanned)
        service.reconcile_resources(snapshot)
        playlist = service.manifest(snapshot)["playlist"]
        for item in playlist.get("items", []):
            item["url"] = _from_manifest_url(str(item.get("url") or ""), sub)
            item.setdefault("auto_title", bool(create_playlist_item(
                title=str(item.get("title") or ""), url=item["url"],
                type=str(item.get("type") or _media_type(item["url"])),
            )["auto_title"]))
        playlist.update({"id": _path_id(sub), "name": sub.name,
                         LINKED_SYNC_STATE: snapshot.to_dict(),
                         "__sync_pending": bool((service.replica.document_id
                                                 and service.replica.pending_count)
                                                or service.replica.waiting_count
                                                or service.resource_errors),
                         "__sync_error": "\n".join(service.resource_errors)})
        return playlist


def stage_manifest_playlist(subfolder_path: str, playlist: dict) -> None:
    """Accept UI intent on local durable storage before scheduling cloud I/O."""
    service = playlist_sync(subfolder_path)
    with service.intent_lock:
        if Path(subfolder_path) != service.folder:
            service.rebase_urls(playlist, Path(subfolder_path))
        service.stage_intent(playlist)


def save_manifest_playlist(subfolder_path: str, pl: dict) -> None:
    """Publish explicit changes relative to the view's causal baseline."""
    sub = Path(subfolder_path)
    service = playlist_sync(sub)
    with service.lock:
        if sub != service.folder:
            service.rebase_urls(pl, sub)
            sub = service.folder
        # Materialization belongs to this background path, never to UI staging.
        for item in pl.get("items", []):
            portable, runtime = portable_playlist_url(str(item.get("url") or ""), sub)
            if runtime is not None:
                item["url"] = runtime
            item[RESOURCE] = portable_resource_key(portable, sub) if portable else ""
        snapshot = service.save(pl)
        service.reconcile_resources(snapshot)
        service.acknowledge_intent(pl)
        if service.replica.pending_count:
            # Keep the UI worker's request alive even after switching folders.
            # The accepted edit is already safe in the local journal outbox.
            raise ManifestWriteError(
                service.replica.operations_dir,
                operation="publish",
                retryable=True,
                cause=OSError("Linked-folder operations are awaiting publication"),
            )


def _manifest_resource_key(url: str, folder: Path) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url.strip()
    resolved = _from_manifest_url(url, folder)
    return os.path.normcase(os.path.normpath(os.path.abspath(resolved)))


def remove_item_from_manifest(
    subfolder_path: str,
    item: Mapping[str, Any],
    *,
    remaining_items: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """
    Remove one playlist occurrence from a watched folder's manifest. Delete
    its physical file only when no remaining occurrence references the media
    and the file resides inside the watched folder.
    
    If the item is a virtual item (e.g. from a `.jwlplaylist`), it is removed
    specifically from the `virtual_items` list of its source file, ensuring
    it doesn't reappear unless the user re-adds the source file.

    Returns True if something was actually cleaned up.
    """
    sub = Path(subfolder_path)
    service = playlist_sync(sub)
    with service.lock:
        playlist = load_manifest_playlist(subfolder_path)
        item_id = str(item.get("id") or "")
        key = portable_resource_key(str(item.get("url") or ""), sub)
        before = playlist["items"]
        playlist["items"] = [candidate for candidate in before if (
            str(candidate.get("id") or "") != item_id if item_id
            else portable_resource_key(str(candidate.get("url") or ""), sub) != key
        )]
        changed = len(playlist["items"]) != len(before)
        snapshot = service.save(playlist)
        # A stale UI occurrence can already have been deleted remotely. Persist
        # resource suppression as well so remaining bytes cannot reimport it.
        if not changed and key:
            desired = dict(snapshot.entities)
            deletion_id, deletion = suppression_record(item_id, key)
            desired[deletion_id] = deletion
            snapshot = service.replica.commit(snapshot, desired)
        if item.get("_virtual") and item.get("_source"):
            source = str(item["_source"])
            entry = service.manifest(snapshot)["processed"].get(source)
            if entry:
                entry["virtual_items"] = [candidate for candidate in entry.get("virtual_items", [])
                                          if str(candidate.get("id") or "") != item_id]
                snapshot = service.update_processed(snapshot, source, entry)
        # Preserve references inserted locally but not yet published by the UI.
        remaining_keys = {portable_resource_key(str(other.get("url") or ""), sub)
                          for other in remaining_items}
        if key not in remaining_keys:
            service.reconcile_resources(snapshot)
        return changed or bool(key)


def _document_cache_key(doc_path: str | Path) -> str:
    path = Path(doc_path)
    safe_name = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in path.name
    ).strip("._")[:48]
    revision = content_signature(path)["sha256"] if path.is_file() else "unavailable"
    digest = hashlib.sha256(f"{path.name}:{revision}".encode("utf-8")).hexdigest()[:16]
    return f"{safe_name or 'document'}-{digest}"


def _page_cache_marker(doc_path: str | Path, dest_dir: str | Path) -> Path:
    return Path(dest_dir) / f".{_document_cache_key(doc_path)}.pages.json"


def _page_cache_signature(doc_path: str | Path) -> dict[str, int | str]:
    path = Path(doc_path)
    return {
        "source": path.name,
        **content_signature(path),
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
    expected_signature: dict | None = None,
) -> list[str]:
    """Render and publish a complete page set without exposing partial output."""
    from solin.core.rendering.pdf import render_pdf_pages_sync

    dest_dir.mkdir(parents=True, exist_ok=True)
    source_signature = _page_cache_signature(source_path)
    if expected_signature is not None and source_signature != expected_signature:
        raise RuntimeError(f"Source changed during conversion: '{source_path.name}'")
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

        final_paths = _publish_staged_outputs(staging_dir, dest_dir, expected_names)
        os.replace(staged_marker, marker)
        return final_paths


def _publish_staged_outputs(
    staging_dir: Path,
    dest_dir: Path,
    output_names: list[str],
) -> list[str]:
    """Publish immutable outputs; only a committed source record exposes them.

    Keep successfully published files after failures: another replica may
    already reference them, even when its metadata has not arrived here yet.
    """
    for output_name in dict.fromkeys(output_names):
        if Path(output_name).name != output_name:
            raise RuntimeError(f"Invalid staged output name: '{output_name}'")
        staged_path = staging_dir / output_name
        if not staged_path.is_file():
            raise RuntimeError(f"Missing staged output: '{output_name}'")
        final_path = dest_dir / output_name
        if final_path.exists():
            if content_signature(final_path) == content_signature(staged_path):
                continue
            raise RuntimeError(f"Conflicting cache output: '{output_name}'")
        os.replace(staged_path, final_path)
    return [str(dest_dir / name) for name in output_names]


def _imported_occurrence_id(source: Path, item: dict, ordinals: dict[str, int]) -> str:
    """Keep an imported occurrence stable when unrelated source items move.

    Native item IDs survive resource replacement. Older readers without IDs
    use the semantic resource plus its repetition number within that resource.
    Titles, trims, resolved CDN URLs and global list positions are not identity.
    """
    native_id = item.get("source_item_id")
    if native_id is not None:
        identity: object = ["native", str(native_id)]
    elif item.get("key_symbol") or item.get("doc_id"):
        identity = ["publication", item.get("type"), item.get("key_symbol") or "",
                    item.get("doc_id") or 0, item.get("track") or 0,
                    item.get("issue_tag") or 0,
                    item.get("meps_language") or item.get("language") or 0]
    elif item.get("data"):
        identity = ["embedded", hashlib.sha256(item["data"]).hexdigest(), item.get("type")]
    else:
        identity = ["resource", item.get("type"), item.get("url") or item.get("jworg_url") or ""]
    key = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    name = json.dumps([resource_identity_key(source.name), key, ordinal], ensure_ascii=False)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"solin:source-occurrence:{name}"))


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
        except Exception as exc:  # noqa: BLE001 - QThread reports unexpected failures via signal
            log.exception("Document conversion worker failed")
            self.conversion_failed.emit(f"Erro inesperado: {exc}")

    def _convert_pdf(self, pdf_path: Path, dest_dir: Path) -> list[str]:
        return _render_document_pages(
            pdf_path,
            pdf_path,
            dest_dir,
            progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
        )

    def _convert_lo(self, lo_path: Path, dest_dir: Path) -> list[str]:
        from solin.core.rendering.libreoffice import libreoffice_path

        source_signature = _page_cache_signature(lo_path)
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
                    raise RuntimeError(
                        f"LibreOffice did not generate a PDF for '{lo_path.name}'."
                    )
                pdf_out = pdfs[0]

            return _render_document_pages(
                lo_path,
                pdf_out,
                dest_dir,
                progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
                expected_signature=source_signature,
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
        resource_lanes: ResourceLaneRegistry,
        resource_claim: ResourceClaim,
        parent=None,
    ):
        super().__init__(parent)
        self._subfolder = subfolder_path
        self._media_lang = media_lang or "E"
        self._fallback_lang = fallback_lang_code or "E"
        self._resource_lanes = resource_lanes
        self._resource_claim = resource_claim

    def run(self) -> None:
        try:
            self._resource_lanes.run(self._resource_claim, self._run_sync)
        except InterruptedError:
            log.info("Watched-folder sync cancelled for %s", self._subfolder)
        except Exception as exc:  # noqa: BLE001 - QThread reports terminal failure via signal
            log.exception("Watched-folder sync thread failed")
            self.sync_failed.emit(str(exc))

    def _run_sync(self) -> None:
        sub = Path(self._subfolder)
        if not sub.is_dir():
            self.sync_failed.emit(f"Folder not found: {sub}")
            return

        self._check_interrupted()
        read_linked_manifest(sub)
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
            try:
                fingerprint = _file_fingerprint(fp)
                outputs, virtuals = self._process_file(fp, cache, ext)

                def validate_conversion(source: Path = fp, expected: dict = fingerprint) -> None:
                    self._check_interrupted()
                    if _file_fingerprint(source) != expected:
                        raise RuntimeError(f"Source changed during conversion: '{source.name}'")

                _commit_processed_entry(sub, fp.name, {
                    "type": ext.lstrip("."),
                    **fingerprint,
                    "outputs": [os.path.basename(o) for o in outputs],
                    "virtual_items": virtuals,
                }, before_commit=validate_conversion)
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 - per-file sync fault isolation
                log.error("Sync failed for %s: %s", fp.name, exc)
                self.progress.emit(fp.name, f"⚠ Error: {str(exc)[:60]}")

        self._check_interrupted()
        self.sync_complete.emit()

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
        from solin.core.rendering.libreoffice import libreoffice_path

        source_signature = _page_cache_signature(lo_path)
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
                expected_signature=source_signature,
            )
            return paths, []

    def _process_jwpub(self, jwpub_path: Path, cache: Path) -> tuple[list[str], list[dict]]:
        self._check_interrupted()
        from solin.core.jw.jwpub_import import (
            JwpubImportRequest,
            JwpubPlaylistImportService,
        )

        with tempfile.TemporaryDirectory(
            prefix=".jwpub-render-",
            dir=cache,
            ignore_cleanup_errors=True,
        ) as tmp:
            staging_dir = Path(tmp)
            items, stem = JwpubPlaylistImportService().read(
                JwpubImportRequest(
                    jwpub_path=str(jwpub_path),
                    language=self._media_lang,
                    dest_images_dir=str(staging_dir),
                    resolve_urls=True,
                )
            )
            outputs = []
            virtuals = []
            ordinals: dict[str, int] = {}
            for item in items:
                self._check_interrupted()
                if item.get("type") == "image" and item.get("url"):
                    image_path = Path(item["url"])
                    if image_path.parent != staging_dir or not image_path.is_file():
                        raise RuntimeError(
                            f"JWPUB image was not staged correctly: '{image_path.name}'"
                        )
                    digest = content_signature(image_path)["sha256"]
                    name = f"image_{digest}{image_path.suffix.lower()}"
                    target = staging_dir / name
                    if not target.exists():
                        os.replace(image_path, target)
                    if name not in outputs:
                        outputs.append(name)
                else:
                    virtuals.append({
                        "id":            _imported_occurrence_id(jwpub_path, item, ordinals),
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
        from solin.core.playlists.reader import read_jwlplaylist
        data = read_jwlplaylist(str(jwl_path), fallback_lang_code=self._fallback_lang)

        with tempfile.TemporaryDirectory(
            prefix=".jwlplaylist-render-",
            dir=cache,
            ignore_cleanup_errors=True,
        ) as tmp:
            staging_dir = Path(tmp)
            outputs = []
            virtuals = []
            embedded_assets: dict[tuple[str, bytes, str], str] = {}
            ordinals: dict[str, int] = {}
            for raw in data.get("items", []):
                self._check_interrupted()
                url = raw.get("url") or raw.get("jworg_url") or ""
                if raw.get("data") and not url:
                    ext = Path(raw.get("filename", "media")).suffix or ".mp4"
                    asset_key = str(raw.get("embedded_asset_key") or "")
                    asset_identity = (
                        asset_key,
                        hashlib.sha256(raw["data"]).digest(),
                        ext.lower(),
                    )
                    fname = embedded_assets.get(asset_identity)
                    if fname is None:
                        fname = f"embedded_{asset_identity[1].hex()}{ext.lower()}"
                        (staging_dir / fname).write_bytes(raw["data"])
                        outputs.append(fname)
                        embedded_assets[asset_identity] = fname
                    url = (Path(CACHE_DIR_NAME) / fname).as_posix()
                virtual_item = {
                    "id":            _imported_occurrence_id(jwl_path, raw, ordinals),
                    "title":         raw.get("title", jwl_path.stem),
                    "url":           url,
                    "type":          raw.get("type", "video"),
                    "key_symbol":    raw.get("key_symbol"),
                    "track":         raw.get("track"),
                    "issue_tag":     raw.get("issue_tag"),
                    "doc_id":        raw.get("doc_id"),
                    "meps_language": raw.get("language", 0),
                }
                for field in (
                    "start_trim_ticks",
                    "end_trim_ticks",
                    "base_duration_ticks",
                    "accuracy",
                    "end_action",
                ):
                    if field in raw:
                        virtual_item[field] = raw[field]
                virtuals.append(virtual_item)
            self._check_interrupted()
            return _publish_staged_outputs(staging_dir, cache, outputs), virtuals

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

        paths_to_watch = [path]
        for sub in Path(path).iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                paths_to_watch.extend(self._document_watch_paths(sub))
        self._watcher.addPaths(paths_to_watch)

    @staticmethod
    def _document_watch_paths(folder: Path) -> list[str]:
        paths = [folder]
        cache = folder / CACHE_DIR_NAME
        if cache.is_dir():
            paths.append(cache)
        sync = folder / ".solin_sync"
        if sync.is_dir():
            paths.append(sync)
            for namespace in sync.iterdir():
                if namespace.is_dir():
                    paths.append(namespace)
                    if namespace.name == "resources":
                        for entry in namespace.iterdir():
                            if entry.is_dir():
                                paths.append(entry)
                                paths.extend(child for child in entry.iterdir() if child.is_dir())
                    operations = namespace / "operations"
                    if operations.is_dir():
                        paths.append(operations)
                    documents = namespace / "documents"
                    if documents.is_dir():
                        paths.append(documents)
        return [str(path) for path in paths]

    def watch_subfolder(self, path: str) -> None:
        """Observe operation arrival as well as visible media and cache changes."""
        folder = Path(path)
        if path and folder.is_dir():
            watched = set(self._watcher.directories())
            missing = [entry for entry in self._document_watch_paths(folder)
                       if entry not in watched]
            if missing:
                self._watcher.addPaths(missing)

    def _document_folder(self, path: Path) -> Path:
        if self._root_path:
            try:
                relative = path.relative_to(Path(self._root_path))
                if relative.parts:
                    return Path(self._root_path) / relative.parts[0]
            except ValueError:
                pass
        for parent in [path, *path.parents]:
            if parent.name in {CACHE_DIR_NAME, ".solin_sync"}:
                return parent.parent
        return path

    def _on_dir_changed(self, path: str) -> None:
        if path == self._root_path:
            self.set_root(self._root_path)
        else:
            folder = self._document_folder(Path(path))
            self.watch_subfolder(str(folder))
            self.subfolder_changed.emit(str(folder))
        self.changed.emit()

    def _on_file_changed(self, path: str) -> None:
        folder = self._document_folder(Path(path).parent)
        self.subfolder_changed.emit(str(folder))
        self.changed.emit()
