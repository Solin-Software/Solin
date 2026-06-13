from .media_library_widget import MediaLibraryWidget


class ClipsWidget(MediaLibraryWidget):
    def __init__(
        self,
        lang_manager,
        cache_manager,
        jw_cache_dir,
        media_ctrl=None,
        parent=None,
    ):
        super().__init__(
            "clips",
            lang_manager,
            cache_manager,
            media_ctrl,
            jw_cache_dir=jw_cache_dir,
            parent=parent,
        )
