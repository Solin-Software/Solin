from app.controllers.language_controller import LanguageController
from app.controllers.main_window_nav import NAV_LABELS


class _LabelStub:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _NavButtonStub:
    def __init__(self):
        self._label = ""
        self.updated = False

    def _update_icon_and_text(self):
        self.updated = True


class _WindowStub:
    def __init__(self):
        self.title = None
        self._sidebar_title_lbl = _LabelStub()
        self._sidebar_subtitle_lbl = _LabelStub()
        for attr, _text in NAV_LABELS:
            setattr(self, attr, _NavButtonStub())

    def tr(self, text):
        return f"tr:{text}"

    def setWindowTitle(self, title):
        self.title = title


def test_retranslate_ui_updates_window_and_sidebar_texts():
    window = _WindowStub()
    controller = LanguageController(window)

    controller.retranslate_ui()

    assert window.title == "tr:Solin"
    assert window._sidebar_title_lbl.text == "tr:Solin"
    assert window._sidebar_subtitle_lbl.text == "tr:Audio & Video"
    assert window.nav_songs_btn._label == "tr:Songs"
    assert window.nav_wifi_btn._label == "tr:Receive via Wi-Fi"
    assert window.nav_songs_btn.updated is True


def test_change_language_retranslates_window_and_sidebar_texts():
    window = _WindowStub()
    controller = LanguageController(window)

    controller.change_language("pt_BR")

    assert window.title == "tr:Solin"
    assert window._sidebar_title_lbl.text == "tr:Solin"
    assert window._sidebar_subtitle_lbl.text == "tr:Audio & Video"
    assert window.nav_settings_btn._label == "tr:Settings"
