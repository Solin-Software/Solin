from dataclasses import dataclass
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtTest import QTest

from solin.core.foundation.settings_store import (
    ProfileAppSettingsStore,
    SettingsNamespace,
    SettingsStore,
)
from solin.core.jw.congregation_lookup_service import CongregationLookupService
from solin.ui.incremental_load import IncrementalLoadState
from solin.widgets import settings_widget as settings_module
from solin.widgets.settings_widget import SettingsWidget


@dataclass(frozen=True)
class _FileSettingsStore(SettingsStore):
    path: Path

    def _settings(self):
        return QSettings(str(self.path), QSettings.Format.IniFormat)


class _SettingsSignals(QObject):
    language_changed = Signal(str)
    media_language_changed = Signal(str)
    screens_changed = Signal()

    @property
    def jw_lang_service(self):
        return self


class _ScenesSettingsWidget(SettingsWidget):
    """Exercise settings construction with only the scenes section enabled."""

    def _init_yearly_text_section(self):
        pass

    def _settings_build_units(self):
        return (self._build_scenes_settings_unit, self._finish_ui_build)

    def _finish_ui_build(self):
        self._ui_ready = True
        self._loading_placeholder.finish()

    def retranslateUi(self):
        self._retranslate_scenes()


@pytest.fixture
def create_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "NATIVE_SCENES_SUPPORTED", True)
    widgets = []

    def create(*, saved: bool, session: bool, build: bool = True):
        store = ProfileAppSettingsStore(
            _FileSettingsStore(
                SettingsNamespace("test-scenes", "settings"),
                tmp_path / f"settings-{len(widgets)}.ini",
            )
        )
        store.set_native_scenes_enabled(saved)
        signals = _SettingsSignals()
        widget = _ScenesSettingsWidget(
            signals,
            signals,
            app_settings=store,
            native_scenes_enabled=session,
            obs_settings=None,
            zoom_settings=None,
            auto_share_settings=None,
            camera_settings=None,
            auto_key_settings=None,
            media_settings=None,
            playback_protection=None,
            meeting_schedule_settings=None,
            watched_folder_settings=None,
            yeartext_settings=None,
            background_song_settings=None,
            remote_control_settings=None,
            remote_control_credentials=None,
            qr_generation_session_factory=None,
            yeartext_service_factory=None,
            congregation_lookup_factory=CongregationLookupService,
            auto_share_accessibility_trusted=lambda: False,
            defer_build=True,
        )
        widgets.append(widget)
        if build:
            widget.preparation_handle.complete_now()
            assert widget.preparation_handle.state is IncrementalLoadState.READY
        return widget, store

    yield create

    for widget in reversed(widgets):
        widget.cleanup()
        shiboken6.delete(widget)


@pytest.mark.parametrize("session", [False, True])
def test_scenes_toggle_preserves_session_and_persists_reversible_changes(create_settings, session):
    widget, store = create_settings(saved=session, session=session, build=False)
    store.set_native_scenes_enabled(not session)

    widget.preparation_handle.complete_now()

    assert widget.preparation_handle.state is IncrementalLoadState.READY
    assert widget._scenes_toggle.is_checked is not session
    assert widget._native_scenes_enabled is session
    assert not widget._scenes_restart_hint.isHidden()

    QTest.mouseClick(widget._scenes_toggle, Qt.MouseButton.LeftButton)

    assert store.native_scenes_enabled() is session
    assert widget._scenes_restart_hint.isHidden()

    QTest.mouseClick(widget._scenes_toggle, Qt.MouseButton.LeftButton)

    assert store.native_scenes_enabled() is not session
    assert widget._native_scenes_enabled is session
    assert not widget._scenes_restart_hint.isHidden()


def test_scenes_section_is_omitted_on_unsupported_platforms(create_settings, monkeypatch):
    monkeypatch.setattr(settings_module, "NATIVE_SCENES_SUPPORTED", False)

    widget, store = create_settings(saved=True, session=False)

    assert not hasattr(widget, "_scenes_toggle")
    assert not hasattr(widget, "_scenes_section_title")
    assert store.native_scenes_enabled() is True
    widget.retranslateUi()
