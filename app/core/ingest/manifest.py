"""Portable manifest helpers for linked folders."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFEST_FILE = "_solin_manifest.json"
CACHE_DIR_NAME = ".solin_cache"
MANIFEST_LOCK = threading.RLock()


class ManifestError(RuntimeError):
    """Raised when a linked-folder manifest cannot be trusted."""


def empty_manifest() -> dict[str, Any]:
    return {"version": 1, "processed": {}}


def to_manifest_url(url: str, subfolder: Path) -> str:
    """Convert a local URL under *subfolder* to a portable relative path."""
    if not url or url.startswith(("http://", "https://")):
        return url
    try:
        rel = Path(url).relative_to(subfolder)
        return rel.as_posix()
    except ValueError:
        return url


def from_manifest_url(url: str, subfolder: Path) -> str:
    """Resolve a manifest URL back to an absolute path on this machine."""
    if not url or url.startswith(("http://", "https://")):
        return url

    p = Path(url)
    if p.is_absolute():
        if p.exists():
            return str(p)
        parts = p.parts
        if len(parts) >= 2 and parts[-2] == CACHE_DIR_NAME:
            tail = Path(parts[-2]) / parts[-1]
        else:
            tail = Path(parts[-1])
        return str(subfolder / tail)

    return str(subfolder / p)


def cache_dir(subfolder: Path) -> Path:
    """Return the .solin_cache directory inside *subfolder*, creating it."""
    d = subfolder / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_manifest(subfolder: Path, *, strict: bool = False) -> dict[str, Any]:
    """Load a linked-folder manifest.

    In non-strict mode this mirrors the historical watched-folder behavior:
    corrupted manifests are logged and treated as empty so source files can be
    reprocessed.  Strict mode is for shared meeting state, where overwriting a
    corrupted manifest would risk data loss.
    """
    mf = subfolder / MANIFEST_FILE
    if not mf.exists():
        return empty_manifest()
    try:
        with mf.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise TypeError("manifest root must be an object")
        if not isinstance(data.get("processed"), dict):
            data["processed"] = {}
        data.setdefault("version", 1)
        return data
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        if strict:
            raise ManifestError(f"Invalid linked-folder manifest: {mf}") from exc
        log.warning("Manifest corrupted in %s; will re-process", subfolder)
        return empty_manifest()


def save_manifest(subfolder: Path, manifest: dict[str, Any]) -> bool:
    """Atomically save a manifest, preserving the previous file on failure."""
    mf = subfolder / MANIFEST_FILE
    temp_path: Path | None = None
    try:
        manifest.setdefault("version", 1)
        subfolder.mkdir(parents=True, exist_ok=True)
        with MANIFEST_LOCK:
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
    except (OSError, TypeError, ValueError) as exc:
        log.error("Cannot write manifest to %s: %s", mf, exc)
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                log.warning("Cannot remove temporary manifest %s", temp_path, exc_info=True)
