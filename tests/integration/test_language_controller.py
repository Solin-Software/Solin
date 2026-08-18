from solin.controllers.language_controller import LanguageContext, LanguageController
from solin.controllers.main_window_nav import NAV_LABELS


class _LabelStub:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _NavButtonStub:
    def __init__(self):
        self.label = ""
        self.updated = False

    def set_label(self, label):
        self.label = label
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


def _controller(window):
    return LanguageController(
        LanguageContext(
            set_window_title=window.setWindowTitle,
            sidebar_title_label=window._sidebar_title_lbl,
            sidebar_subtitle_label=window._sidebar_subtitle_lbl,
            nav_buttons={attr: getattr(window, attr) for attr, _source_text in NAV_LABELS},
            translate=window.tr,
        )
    )


def test_retranslate_ui_updates_window_and_sidebar_texts():
    window = _WindowStub()
    controller = _controller(window)

    controller.retranslate_ui()

    assert window.title == "tr:Solin"
    assert window._sidebar_title_lbl.text == "tr:Solin"
    assert window._sidebar_subtitle_lbl.text == "tr:Audio & Video"
    assert window.nav_library_btn.label == "tr:Library"
    assert window.nav_wifi_btn.label == "tr:Receive via Wi-Fi"
    assert window.nav_library_btn.updated is True


def test_change_language_retranslates_window_and_sidebar_texts():
    window = _WindowStub()
    controller = _controller(window)

    controller.change_language("pt_BR")

    assert window.title == "tr:Solin"
    assert window._sidebar_title_lbl.text == "tr:Solin"
    assert window._sidebar_subtitle_lbl.text == "tr:Audio & Video"
    assert window.nav_settings_btn.label == "tr:Settings"


def test_language_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
