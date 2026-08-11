from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    log_dir: Path
    cache_dir: Path
    media_cache_dir: Path
    thumb_cache_dir: Path
    meeting_thumb_cache_dir: Path
    jwpub_cache_dir: Path
    pdf_pages_dir: Path
    pptx_pages_dir: Path
    docx_pages_dir: Path

    @classmethod
    def from_roots(
        cls,
        *,
        data_dir: str | Path,
        cache_dir: str | Path,
    ) -> RuntimePaths:
        data_root = Path(data_dir)
        cache_root = Path(cache_dir)
        return cls(
            data_dir=data_root,
            log_dir=data_root / "logs",
            cache_dir=cache_root,
            media_cache_dir=cache_root / "media",
            thumb_cache_dir=cache_root / "thumbs",
            meeting_thumb_cache_dir=cache_root / "meeting_thumbs",
            jwpub_cache_dir=cache_root / "jwpub",
            pdf_pages_dir=cache_root / "pdf_pages",
            pptx_pages_dir=cache_root / "pptx_pages",
            docx_pages_dir=cache_root / "docx_pages",
        )

    @classmethod
    def from_standard_locations(cls) -> RuntimePaths:
        from PySide6.QtCore import QStandardPaths

        location = QStandardPaths.StandardLocation
        return cls.from_roots(
            data_dir=QStandardPaths.writableLocation(location.AppDataLocation),
            cache_dir=QStandardPaths.writableLocation(location.CacheLocation),
        )

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.log_dir,
            self.cache_dir,
            self.media_cache_dir,
            self.thumb_cache_dir,
            self.meeting_thumb_cache_dir,
            self.jwpub_cache_dir,
            self.pdf_pages_dir,
            self.pptx_pages_dir,
            self.docx_pages_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    profile_dir: Path
    profile_cache_dir: Path
    playlists_file: Path
    meeting_trees_file: Path
    pending_deletions_file: Path
    images_dir: Path
    embedded_dir: Path
    thumb_cache_dir: Path
    meeting_thumb_cache_dir: Path
    pdf_pages_dir: Path
    pptx_pages_dir: Path
    docx_pages_dir: Path
    native_webview_data_dir: Path
    native_webview_cache_dir: Path | None

    @property
    def native_webview_data_root(self) -> Path:
        return self.native_webview_data_dir.parent.parent

    @property
    def talk_theme_file(self) -> Path:
        return self.profile_dir / "talk_theme.json"

    @property
    def talk_theme_assets_dir(self) -> Path:
        return self.profile_dir / "talk_theme_assets"

    @property
    def scenes_file(self) -> Path:
        return self.profile_dir / "scenes.json"

    @property
    def scene_profiles_catalog_file(self) -> Path:
        return self.profile_dir / "scene_profiles.json"

    @property
    def scene_profiles_dir(self) -> Path:
        return self.profile_dir / "scene_profiles"

    @property
    def scene_resources_file(self) -> Path:
        return self.profile_dir / "scene_resources.json"

    @property
    def scenes_runtime_file(self) -> Path:
        return self.profile_dir / "scenes_runtime.json"

    @property
    def scenes_assets_dir(self) -> Path:
        return self.profile_dir / "scenes_assets"

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
        profile_cache_dir = (
            cache_root / "profiles" / profile_id
            if cache_root is not None
            else profile_dir / "cache"
        )
        return cls(
            profile_dir=profile_dir,
            profile_cache_dir=profile_cache_dir,
            playlists_file=profile_dir / "playlists.json",
            meeting_trees_file=profile_dir / "meeting_trees.json",
            pending_deletions_file=profile_dir / "pending_cleanup.json",
            images_dir=profile_dir / "images",
            embedded_dir=profile_dir / "embedded",
            thumb_cache_dir=profile_cache_dir / "thumbs",
            meeting_thumb_cache_dir=profile_cache_dir / "meeting_thumbs",
            pdf_pages_dir=profile_cache_dir / "pdf_pages",
            pptx_pages_dir=profile_cache_dir / "pptx_pages",
            docx_pages_dir=profile_cache_dir / "docx_pages",
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
            self.talk_theme_assets_dir,
            self.scenes_assets_dir,
            self.scene_profiles_dir,
            self.profile_cache_dir,
            self.thumb_cache_dir,
            self.meeting_thumb_cache_dir,
            self.pdf_pages_dir,
            self.pptx_pages_dir,
            self.docx_pages_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
