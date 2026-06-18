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
        with tmp.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=indent,
                sort_keys=sort_keys,
            )
            if trailing_newline:
                file.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
