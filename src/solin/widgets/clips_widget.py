from __future__ import annotations

from typing import TYPE_CHECKING

from .media_library_widget import MediaLibraryWidget

if TYPE_CHECKING:
    from ..core.jw.clip_fetch import ClipFetchThreadFactory


class ClipsWidget(MediaLibraryWidget):
    def __init__(
        self,
        lang_manager,
        cache_manager,
        jw_cache_dir,
        clip_fetch_thread_factory: ClipFetchThreadFactory,
        media_ctrl=None,
        parent=None,
    ):
        super().__init__(
            "clips",
            lang_manager,
            cache_manager,
            media_ctrl,
            clip_fetch_thread_factory=clip_fetch_thread_factory,
            jw_cache_dir=jw_cache_dir,
            parent=parent,
        )
