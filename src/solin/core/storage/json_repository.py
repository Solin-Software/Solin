from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from solin.core.storage.json_files import read_json_file, write_json_atomic


class JsonFileRepository:
    """Small filesystem repository for one JSON document."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> Any:
        return read_json_file(self._path)

    def write(
        self,
        data: Any,
        *,
        indent: int = 2,
        sort_keys: bool = False,
        trailing_newline: bool = False,
    ) -> None:
        write_json_atomic(
            self._path,
            data,
            indent=indent,
            sort_keys=sort_keys,
            trailing_newline=trailing_newline,
        )
