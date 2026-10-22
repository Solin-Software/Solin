"""Still images of PTZ camera presets, one JPEG per preset id.

Kept beside the scene documents rather than inside them: a thumbnail is a
convenience for choosing a preset, not part of what a preset means, so it needs
no document schema and a missing or unreadable file only costs a placeholder.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import tempfile

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)

# A 16:9 still a few hundred pixels wide is a few tens of kilobytes; refuse
# anything far beyond that rather than load an arbitrary file into the UI.
MAXIMUM_THUMBNAIL_BYTES = 2 * 1024 * 1024
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_JPEG_QUALITY = 85


class PresetThumbnailStore:
    """Reads and writes preset stills under one directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, preset_id: str) -> Path | None:
        # Preset ids are document identities; anything else must never become a path.
        if not isinstance(preset_id, str) or not _SAFE_IDENTITY.fullmatch(preset_id):
            return None
        if preset_id in {".", ".."}:
            return None
        return self._directory / f"{preset_id}.jpg"

    def load(self, preset_id: str) -> bytes | None:
        path = self._path(preset_id)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAXIMUM_THUMBNAIL_BYTES:
                return None
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            log.debug("Could not read the preset thumbnail", exc_info=True)
            return None

    def save(self, preset_id: str, data: bytes) -> bool:
        path = self._path(preset_id)
        if path is None or not data or len(data) > MAXIMUM_THUMBNAIL_BYTES:
            return False
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            # Write beside the target and swap it in, so a crash never leaves a
            # half-written image behind.
            handle, temporary = tempfile.mkstemp(dir=self._directory, suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, path)
            except BaseException:  # noqa: BLE001 - remove the partial file, then re-raise
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError:
            log.warning("Could not save the preset thumbnail", exc_info=True)
            return False
        return True

    def save_image(self, preset_id: str, image: QImage) -> bool:
        """Encode a still as JPEG and save it."""
        if image.isNull():
            return False
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return False
        try:
            # PySide6 takes the format as text at runtime; its stub says bytes.
            if not image.save(buffer, "JPG", _JPEG_QUALITY):  # pyright: ignore[reportArgumentType, reportCallIssue]
                return False
        finally:
            buffer.close()
        return self.save(preset_id, bytes(data.data()))

    def load_image(self, preset_id: str) -> QImage | None:
        data = self.load(preset_id)
        if not data:
            return None
        image = QImage.fromData(data)
        return None if image.isNull() else image

    def delete(self, preset_id: str) -> None:
        path = self._path(preset_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.debug("Could not delete the preset thumbnail", exc_info=True)
