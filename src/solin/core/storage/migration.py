from __future__ import annotations

import shutil
from pathlib import Path


def merge_dirs(src: Path, dst: Path) -> None:
    """Copy src into dst, preserving conflicting names with legacy suffixes."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_file():
            shutil.copy2(item, unique_destination(target))
        elif item.is_dir():
            if target.exists() and target.is_dir():
                merge_dirs(item, target)
            else:
                shutil.copytree(item, unique_destination(target))


def unique_destination(path: Path) -> Path:
    """Return a free path, adding a _legacy_N suffix when needed."""
    if not path.exists():
        return path

    suffix = path.suffix
    stem = path.stem
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}_legacy_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def move_file_if_exists(src: Path, dst: Path) -> bool:
    """Copy a legacy file to dst and remove the source after success."""
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, unique_destination(dst))
    src.unlink()
    return True


def move_dir_if_exists(src: Path, dst: Path) -> bool:
    """Move or merge a legacy directory into dst."""
    if not src.exists() or not src.is_dir():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.move(str(src), str(dst))
    else:
        merge_dirs(src, dst)
        shutil.rmtree(src, ignore_errors=True)
    return True
