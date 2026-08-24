from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def read_json_file(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json_atomic(
    path: str | Path,
    data: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    trailing_newline: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=indent,
                sort_keys=sort_keys,
            )
            if trailing_newline:
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, target)
        _sync_parent_directory(target.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _sync_parent_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
