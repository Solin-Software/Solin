import inspect
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from solin.styles.theme import get_theme
from solin.ui.controls import NoScrollComboBox
from solin.widgets.settings.about_section import AboutSectionMixin
from solin.widgets.settings.auto_keys_section import AutoKeysSectionMixin
from solin.widgets.settings.auto_share_section import AutoShareSectionMixin
from solin.widgets.settings.camera_section import CameraSectionMixin
from solin.widgets.settings.language_section import LanguageSectionMixin
from solin.widgets.settings.layout_helpers import SettingsLayoutMixin
from solin.widgets.settings.media_section import MediaSectionMixin
from solin.widgets.settings.obs_section import ObsSectionMixin
from solin.widgets.settings.screens_section import ScreensSectionMixin
from solin.widgets.settings.shared import SettingsToggleSwitch
from solin.widgets.settings.watched_folder_section import WatchedFolderSectionMixin
from solin.widgets.settings.yearly_text_section import YearlyTextSectionMixin
from solin.widgets.settings.zoom_section import ZoomSectionMixin
from solin.widgets import settings_widget as settings_widget_module
from solin.widgets.settings_widget import SettingsWidget


_APP = QApplication.instance() or QApplication([])


def test_settings_shared_visual_contracts_are_public():
    from solin.widgets.settings import shared as settings_shared
    from solin.widgets.settings.auto_key_dialog import AutoKeyEditorDialog

    assert settings_shared.SETTINGS_BG == "#0d1117"
    assert settings_shared.SETTINGS_BORDER_STRONG == "#30363d"
    assert settings_shared.SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET.startswith(
        "QPushButton"
    )
    assert settings_shared.SettingsToggleSwitch.__name__ == "SettingsToggleSwitch"
    assert AutoKeyEditorDialog.__name__ == "AutoKeyEditorDialog"


def test_settings_widget_uses_shared_layout_helpers():
    assert issubclass(SettingsWidget, SettingsLayoutMixin)
    assert SettingsWidget._section_title is SettingsLayoutMixin._section_title
    assert SettingsWidget._card is SettingsLayoutMixin._card
    assert SettingsWidget._divider is SettingsLayoutMixin._divider
    assert SettingsWidget._toggle_row is SettingsLayoutMixin._toggle_row
    assert SettingsWidget._clickable_row is SettingsLayoutMixin._clickable_row


class _ThemeSelectorComboStub:
    def __init__(self):
        self.items = []
        self.current_index = -1
        self.visible = None
        self.signals_blocked = False

    def blockSignals(self, blocked):
        self.signals_blocked = blocked

    def clear(self):
        self.items.clear()

    def addItem(self, text, data):
        self.items.append((text, data))

    def findData(self, data):
        for index, (_text, item_data) in enumerate(self.items):
            if item_data == data:
                return index
        return -1

    def itemData(self, index):
        return self.items[index][1]

    def setCurrentIndex(self, index):
        self.current_index = index

    def setVisible(self, visible):
        self.visible = visible


class _AppSettingsStub:
    def __init__(self, theme_id="dark"):
        self._theme_id = theme_id
        self.saved = []

    def app_theme_id(self):
        return self._theme_id

    def set_app_theme_id(self, theme_id):
        self.saved.append(theme_id)
        self._theme_id = theme_id


class _ThemeSelectorWidgetStub:
    def __init__(self, theme_id="dark"):
        self._theme_combo = _ThemeSelectorComboStub()
        self._app_settings = _AppSettingsStub(theme_id)
        self.translated: list[str] = []

    def tr(self, text):
        self.translated.append(text)
        return text

    def _theme_display_name(self, theme):
        return SettingsWidget._theme_display_name(self, theme)


class _ThemeSelectorBuildHarness(QWidget):
    def __init__(self, theme_id="dark"):
        super().__init__()
        self._app_settings = _AppSettingsStub(theme_id)

    def tr(self, text):
        return text

    def _populate_theme_selector(self):
        SettingsWidget._populate_theme_selector(self)

    def _on_theme_selected(self, index):
        SettingsWidget._on_theme_selected(self, index)

    def _theme_display_name(self, theme):
        return SettingsWidget._theme_display_name(self, theme)


class _ApplyThemeHarness(QWidget):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []
        self._yt_status_kind = "success"
        self._yt_status_text = "Annual text updated for 2026"

    def _apply_settings_theme_bindings(self):
        self.calls.append("bindings")

    def _build_ui(self):
        raise AssertionError("apply_theme must not rebuild Settings UI")

    def retranslateUi(self):
        raise AssertionError("apply_theme must not retranslate Settings UI")

    def _apply_yearly_text_theme(self):
        assert self._yt_status_kind == "success"
        assert self._yt_status_text == "Annual text updated for 2026"
        self.calls.append("yearly")

    def _apply_obs_theme(self):
        self.calls.append("obs")

    def _apply_auto_share_theme(self):
        self.calls.append("auto_share")

    def _apply_auto_keys_theme(self):
        self.calls.append("auto_keys")

    def _apply_watched_folder_theme(self):
        self.calls.append("watched_folder")

    def _apply_about_theme(self):
        self.calls.append("about")

    def _apply_zoom_theme(self):
        self.calls.append("zoom")

    def _apply_meeting_schedule_theme(self):
        self.calls.append("meeting_schedule")

    def _apply_screens_theme(self):
        self.calls.append("screens")


class _TopLevelShowRecorder(QObject):
    def __init__(self):
        super().__init__()
        self.shown_top_levels: list[str] = []

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.Show
            and isinstance(obj, QWidget)
            and obj.isWindow()
        ):
            self.shown_top_levels.append(type(obj).__name__)
        return False


def test_settings_theme_selector_hides_when_only_one_theme(monkeypatch):
    monkeypatch.setattr(
        settings_widget_module,
        "available_themes",
        lambda: (get_theme("dark"),),
    )
    widget = _ThemeSelectorWidgetStub()

    SettingsWidget._populate_theme_selector(widget)

    assert widget._theme_combo.items == [("Dark", "dark")]
    assert widget._theme_combo.visible is False


def test_settings_theme_selector_lists_available_themes_and_saves_choice():
    widget = _ThemeSelectorWidgetStub("light")

    SettingsWidget._populate_theme_selector(widget)

    assert widget._theme_combo.items == [("Dark", "dark"), ("Light", "light")]
    assert widget._theme_combo.current_index == 1
    assert widget._theme_combo.visible is True

    SettingsWidget._on_theme_selected(widget, 0)

    assert widget._app_settings.saved == ["dark"]


def test_settings_builtin_theme_names_are_lupdate_visible_literals():
    source = inspect.getsource(SettingsWidget._theme_display_name)

    assert 'self.tr("Dark")' in source
    assert 'self.tr("Light")' in source


def test_settings_apply_theme_hooks_are_implemented():
    hook_names = (
        "_apply_yearly_text_theme",
        "_apply_obs_theme",
        "_apply_auto_share_theme",
        "_apply_auto_keys_theme",
        "_apply_watched_folder_theme",
        "_apply_about_theme",
        "_apply_zoom_theme",
        "_apply_meeting_schedule_theme",
        "_apply_screens_theme",
    )

    for hook_name in hook_names:
        assert hasattr(SettingsWidget, hook_name)


def test_settings_apply_theme_preserves_state_instead_of_rebuilding():
    widget = _ApplyThemeHarness()

    SettingsWidget.apply_theme(widget)

    assert widget.calls == [
        "bindings",
        "yearly",
        "obs",
        "auto_share",
        "auto_keys",
        "watched_folder",
        "about",
        "zoom",
        "meeting_schedule",
        "screens",
    ]
    assert widget._yt_status_kind == "success"
    assert widget._yt_status_text == "Annual text updated for 2026"


def test_settings_theme_selector_is_parented_before_visibility_changes():
    recorder = _TopLevelShowRecorder()
    _APP.installEventFilter(recorder)
    try:
        widget = _ThemeSelectorBuildHarness()
        header = QHBoxLayout(widget)

        SettingsWidget._build_theme_selector(widget, header)
        _APP.processEvents()
    finally:
        _APP.removeEventFilter(recorder)

    assert widget._theme_combo.parent() is widget
    assert isinstance(widget._theme_combo, NoScrollComboBox)
    assert recorder.shown_top_levels == []


def test_settings_widget_uses_language_section_mixin():
    assert issubclass(SettingsWidget, LanguageSectionMixin)
    assert SettingsWidget._build_lang_card is LanguageSectionMixin._build_lang_card
    assert SettingsWidget._refresh_lang_row is LanguageSectionMixin._refresh_lang_row
    assert (
        SettingsWidget._refresh_media_lang_row
        is LanguageSectionMixin._refresh_media_lang_row
    )
    assert SettingsWidget._select_lang is LanguageSectionMixin._select_lang


def test_settings_jw_media_language_uses_media_icon():
    source = Path("src/solin/widgets/settings/language_section.py").read_text(
        encoding="utf-8"
    )

    assert "ICON_MEDIA_LANGUAGE" in source
    assert "ICON_BOOK" not in source


def test_settings_widget_uses_media_section_mixin():
    assert issubclass(SettingsWidget, MediaSectionMixin)
    assert SettingsWidget._build_media_card is MediaSectionMixin._build_media_card
    assert (
        SettingsWidget.get_auto_download_on_play
        is MediaSectionMixin.get_auto_download_on_play
    )
    assert (
        SettingsWidget.get_meetings_auto_download
        is MediaSectionMixin.get_meetings_auto_download
    )
    assert SettingsWidget.get_sjjm_announce_mode is MediaSectionMixin.get_sjjm_announce_mode
    assert SettingsWidget.get_start_videos_paused is MediaSectionMixin.get_start_videos_paused
    assert (
        SettingsWidget.get_playback_protection_enabled
        is MediaSectionMixin.get_playback_protection_enabled
    )


def test_playback_protection_toggle_tracks_runtime_controller_changes():
    toggle = SettingsToggleSwitch(checked=False)
    host = type("_Host", (), {})()
    host._playback_protection_toggle = toggle
    host._playback_protection = type("_Protection", (), {"enabled": True})()

    MediaSectionMixin._sync_playback_protection_toggle(host)

    assert toggle.is_checked is True
    assert toggle._anim_value == 1.0


def test_settings_widget_uses_obs_section_mixin():
    assert issubclass(SettingsWidget, ObsSectionMixin)
    assert SettingsWidget._build_obs_card is ObsSectionMixin._build_obs_card
    assert SettingsWidget._sync_obs_ui_state is ObsSectionMixin._sync_obs_ui_state
    assert SettingsWidget.get_obs_default_scene is ObsSectionMixin.get_obs_default_scene
    assert (
        SettingsWidget.get_obs_media_window_scene
        is ObsSectionMixin.get_obs_media_window_scene
    )
    assert SettingsWidget.get_obs_ndi_enabled is ObsSectionMixin.get_obs_ndi_enabled
    assert SettingsWidget.get_obs_ndi_source is ObsSectionMixin.get_obs_ndi_source


def test_settings_widget_uses_auto_share_section_mixin():
    assert issubclass(SettingsWidget, AutoShareSectionMixin)
    assert (
        SettingsWidget._build_auto_share_card
        is AutoShareSectionMixin._build_auto_share_card
    )
    assert SettingsWidget._autoshare_hotkey is AutoShareSectionMixin._autoshare_hotkey
    assert (
        SettingsWidget._refresh_autoshare_accessibility_status
        is AutoShareSectionMixin._refresh_autoshare_accessibility_status
    )


def test_settings_widget_uses_camera_section_mixin():
    assert issubclass(SettingsWidget, CameraSectionMixin)
    assert SettingsWidget._build_camera_card is CameraSectionMixin._build_camera_card
    assert SettingsWidget.get_camera_enabled is CameraSectionMixin.get_camera_enabled


def test_settings_widget_uses_zoom_section_mixin():
    assert issubclass(SettingsWidget, ZoomSectionMixin)
    assert SettingsWidget._build_zoom_card is ZoomSectionMixin._build_zoom_card
    assert (
        SettingsWidget._on_zoom_enabled_toggled
        is ZoomSectionMixin._on_zoom_enabled_toggled
    )
    assert SettingsWidget._on_zoom_parts_toggled is ZoomSectionMixin._on_zoom_parts_toggled


def test_settings_widget_uses_screens_section_mixin():
    assert issubclass(SettingsWidget, ScreensSectionMixin)
    assert SettingsWidget._populate_screens is ScreensSectionMixin._populate_screens
    assert SettingsWidget._refresh_screens is ScreensSectionMixin._refresh_screens


def test_settings_widget_uses_about_section_mixin():
    assert issubclass(SettingsWidget, AboutSectionMixin)
    assert SettingsWidget._build_about_card is AboutSectionMixin._build_about_card


def test_settings_widget_uses_yearly_text_section_mixin():
    assert issubclass(SettingsWidget, YearlyTextSectionMixin)
    assert (
        SettingsWidget._init_yearly_text_section
        is YearlyTextSectionMixin._init_yearly_text_section
    )
    assert (
        SettingsWidget._build_yearly_text_card
        is YearlyTextSectionMixin._build_yearly_text_card
    )
    assert SettingsWidget.get_yearly_text is YearlyTextSectionMixin.get_yearly_text


def test_settings_widget_uses_auto_keys_section_mixin():
    assert issubclass(SettingsWidget, AutoKeysSectionMixin)
    assert SettingsWidget._build_auto_keys_card is AutoKeysSectionMixin._build_auto_keys_card
    assert SettingsWidget._event_label is AutoKeysSectionMixin._event_label
    assert SettingsWidget._auto_key_icon_button is AutoKeysSectionMixin._auto_key_icon_button


def test_settings_widget_uses_watched_folder_section_mixin():
    assert issubclass(SettingsWidget, WatchedFolderSectionMixin)
    assert (
        SettingsWidget._build_watched_folder_card
        is WatchedFolderSectionMixin._build_watched_folder_card
    )
    assert (
        SettingsWidget._sync_watched_folder_path_label
        is WatchedFolderSectionMixin._sync_watched_folder_path_label
    )
    assert SettingsWidget.get_watched_folder is WatchedFolderSectionMixin.get_watched_folder
