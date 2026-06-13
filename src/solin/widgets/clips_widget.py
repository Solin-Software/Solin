from .media_library_widget import MediaLibraryWidget


class ClipsWidget(MediaLibraryWidget):
    def __init__(self, lang_manager, media_ctrl=None, parent=None):
        super().__init__("clips", lang_manager, media_ctrl, parent)
