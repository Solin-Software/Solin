"""Atomic storage for profile-owned media artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

from solin.core.media.download_storage import make_persistent_temp_path, safe_remove

_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class ProfileMediaStore:
    """Persists generated and imported media under the active profile."""

    def __init__(self, embedded_dir: str | Path, images_dir: str | Path) -> None:
        self._embedded_dir = Path(embedded_dir)
        self._images_dir = Path(images_dir)

    @property
    def embedded_dir(self) -> Path:
        return self._embedded_dir

    @property
    def images_dir(self) -> Path:
        return self._images_dir

    def save_embedded(
        self,
        data: bytes,
        filename_hint: str = "media",
        *,
        identifier: str | None = None,
        default_suffix: str = ".mp4",
    ) -> str:
        suffix = self._safe_suffix(Path(filename_hint).suffix, default_suffix)
        return self._write(
            self._embedded_dir,
            f"{self._safe_identifier(identifier)}{suffix}",
            data,
        )

    def save_projected_image(self, data: bytes) -> str:
        return self._write(
            self._images_dir,
            f"{uuid.uuid4().hex}.png",
            data,
        )

    @staticmethod
    def _safe_identifier(identifier: str | None) -> str:
        if identifier:
            sanitized = _SAFE_IDENTIFIER.sub("_", identifier).strip("_-")
            if sanitized:
                return sanitized[:120]
        return uuid.uuid4().hex

    @staticmethod
    def _safe_suffix(suffix: str, fallback: str) -> str:
        normalized = suffix.lower()
        if _SAFE_SUFFIX.fullmatch(normalized):
            return normalized
        fallback_normalized = fallback.lower()
        if _SAFE_SUFFIX.fullmatch(fallback_normalized):
            return fallback_normalized
        raise ValueError(f"Invalid fallback media suffix: {fallback}")

    @staticmethod
    def _write(directory: Path, filename: str, data: bytes) -> str:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        write_path = make_persistent_temp_path(destination)
        try:
            with open(write_path, "wb") as handle:
                handle.write(data)
            os.replace(write_path, destination)
        except OSError:
            safe_remove(write_path)
            raise
        return os.fspath(destination)
