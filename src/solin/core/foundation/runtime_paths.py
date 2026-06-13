from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    pending_del_file: Path
    log_dir: Path
    cache_dir: Path
    media_cache_dir: Path
    thumb_cache_dir: Path
    meeting_thumb_cache_dir: Path
    pdf_pages_dir: Path
    pptx_pages_dir: Path
    docx_pages_dir: Path

    @classmethod
    def from_legacy_globals(cls) -> RuntimePaths:
        from solin.core.foundation import paths

        if not paths.DATA_DIR or not paths.CACHE_DIR:
            raise RuntimeError("Runtime paths requested before paths.init().")

        return cls(
            data_dir=Path(paths.DATA_DIR),
            pending_del_file=Path(paths.PENDING_DEL_FILE),
            log_dir=Path(paths.LOG_DIR),
            cache_dir=Path(paths.CACHE_DIR),
            media_cache_dir=Path(paths.MEDIA_CACHE_DIR),
            thumb_cache_dir=Path(paths.THUMB_CACHE_DIR),
            meeting_thumb_cache_dir=Path(paths.MEETING_THUMB_CACHE_DIR),
            pdf_pages_dir=Path(paths.PDF_PAGES_DIR),
            pptx_pages_dir=Path(paths.PPTX_PAGES_DIR),
            docx_pages_dir=Path(paths.DOCX_PAGES_DIR),
        )

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.log_dir,
            self.cache_dir,
            self.media_cache_dir,
            self.thumb_cache_dir,
            self.meeting_thumb_cache_dir,
            self.pdf_pages_dir,
            self.pptx_pages_dir,
            self.docx_pages_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    profile_dir: Path
    playlists_file: Path
    meeting_trees_file: Path
    images_dir: Path
    embedded_dir: Path
    native_webview_data_dir: Path
    native_webview_cache_dir: Path | None

    @property
    def native_webview_data_root(self) -> Path:
        return self.native_webview_data_dir.parent.parent

    @classmethod
    def from_roots(
        cls,
        *,
        data_dir: str | Path,
        cache_dir: str | Path | None,
        profile_id: str,
    ) -> ProfilePaths:
        data_root = Path(data_dir)
        profile_dir = data_root / "profiles" / profile_id
        cache_root = Path(cache_dir) if cache_dir else None
        return cls(
            profile_dir=profile_dir,
            playlists_file=profile_dir / "playlists.json",
            meeting_trees_file=profile_dir / "meeting_trees.json",
            images_dir=profile_dir / "images",
            embedded_dir=profile_dir / "embedded",
            native_webview_data_dir=(
                data_root / "NativeWebView" / "sessions" / f"solin_session_{profile_id}"
            ),
            native_webview_cache_dir=(
                cache_root / "NativeWebView" / "sessions" / f"solin_session_{profile_id}"
                if cache_root is not None
                else None
            ),
        )

    def ensure_dirs(self) -> None:
        for directory in (
            self.profile_dir,
            self.images_dir,
            self.embedded_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
