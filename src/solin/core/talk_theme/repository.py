from __future__ import annotations

import hashlib
import os
import uuid
import warnings
from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from solin.core.storage.json_repository import JsonFileRepository

from .models import TalkThemeLibrary
from .presets import default_document


class TalkThemeRepository:
    """Per-profile persistence for explicitly saved user presets."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._repository = JsonFileRepository(path)

    @property
    def path(self) -> Path:
        return self._repository.path

    def load(self) -> TalkThemeLibrary:
        fallback = default_document()
        try:
            raw = self._repository.read()
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return TalkThemeLibrary(document=fallback)
        return TalkThemeLibrary.from_record(raw, fallback_document=fallback)

    def save(self, library: TalkThemeLibrary) -> None:
        self._repository.write(
            library.to_record(),
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )


class TalkThemeAssetErrorCode(StrEnum):
    UNSUPPORTED_FORMAT = "unsupported_format"
    ANIMATED_IMAGE = "animated_image"
    OPEN_FAILED = "open_failed"
    INVALID_DIMENSIONS = "invalid_dimensions"


class TalkThemeAssetError(ValueError):
    def __init__(self, code: TalkThemeAssetErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class TalkThemeAssetStore:
    """Imports background images as immutable, content-addressed profile assets."""

    _SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

    def __init__(self, directory: str | os.PathLike[str], *, max_long_edge: int = 4096) -> None:
        self._directory = Path(directory)
        self._max_long_edge = max(512, int(max_long_edge))

    @property
    def directory(self) -> Path:
        return self._directory

    def resolve(self, name: str) -> Path | None:
        candidate = self._directory / Path(name).name
        return candidate if candidate.is_file() else None

    def import_image(self, source: str | os.PathLike[str]) -> Path:
        source_path = Path(source)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source_path) as opened:
                    image_format = str(opened.format or "").upper()
                    if image_format not in self._SUPPORTED_FORMATS:
                        raise TalkThemeAssetError(TalkThemeAssetErrorCode.UNSUPPORTED_FORMAT)
                    if bool(getattr(opened, "is_animated", False)):
                        raise TalkThemeAssetError(TalkThemeAssetErrorCode.ANIMATED_IMAGE)
                    image = ImageOps.exif_transpose(opened)
                    image.load()
        except TalkThemeAssetError:
            raise
        except (
            FileNotFoundError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise TalkThemeAssetError(TalkThemeAssetErrorCode.OPEN_FAILED) from exc

        width, height = image.size
        if width <= 0 or height <= 0:
            raise TalkThemeAssetError(TalkThemeAssetErrorCode.INVALID_DIMENSIONS)
        longest = max(width, height)
        if longest > self._max_long_edge:
            scale = self._max_long_edge / longest
            image = image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.LANCZOS,
            )

        has_alpha = "A" in image.getbands()
        extension = ".png" if has_alpha else ".jpg"
        mode = "RGBA" if has_alpha else "RGB"
        if image.mode != mode:
            image = image.convert(mode)

        self._directory.mkdir(parents=True, exist_ok=True)
        temp = self._directory / f".import-{uuid.uuid4().hex}{extension}"
        try:
            if has_alpha:
                image.save(temp, format="PNG", optimize=True)
            else:
                image.save(temp, format="JPEG", quality=92, optimize=True, progressive=True)
            payload = temp.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            target = self._directory / f"{digest}{extension}"
            if target.exists():
                return target
            os.replace(temp, target)
            return target
        finally:
            temp.unlink(missing_ok=True)

    def prune_unreferenced(self, referenced_names: set[str]) -> tuple[Path, ...]:
        """Remove managed assets only after their owning document was persisted."""

        if not self._directory.is_dir():
            return ()
        retained = {Path(name).name for name in referenced_names if name}
        removed: list[Path] = []
        for candidate in self._directory.iterdir():
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            if candidate.name in retained:
                continue
            try:
                candidate.unlink()
            except OSError:
                continue
            removed.append(candidate)
        return tuple(removed)
