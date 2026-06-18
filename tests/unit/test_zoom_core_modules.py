from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from solin.core.integrations.automation.zoom.controls import ZoomSession
from solin.core.integrations.automation.zoom.i18n_labels import (
    AUDIO_MUTED_TEXT,
    AUDIO_UNMUTED_TEXT,
    CONNECT_AUDIO_TEXT,
    PARTICIPANTS_TEXT,
    PEOPLE_SEPARATORS,
    VIDEO_STARTED_TEXT,
    VIDEO_STOPPED_TEXT,
)
from solin.core.integrations.automation.zoom.toolbar_cache import (
    PARTICIPANTS_PANEL_CONTROL_IDS,
    TOOLBAR_CORE_CONTROL_IDS,
    ToolbarCache,
)
from solin.core.integrations.automation.zoom.text_match import (
    _audio_button_state_from_text,
    _matches_toolbar_action,
    _normalize_toolbar_text,
    _toolbar_action_match_score,
    _video_button_state_from_text,
)
from solin.core.integrations.automation.zoom.state import (
    AudioState,
    MeetingState,
    ShareState,
    VideoState,
)


def test_zoom_state_types_are_importable_without_pywinauto():
    state = MeetingState()

    assert state.audio is AudioState.UNKNOWN
    assert state.video is VideoState.UNKNOWN
    assert state.sharing is ShareState.UNKNOWN
    assert state.participant_names == ()


def test_zoom_controls_import_without_pywinauto():
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockPywinauto(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "pywinauto" or fullname.startswith("pywinauto."):
                    raise ImportError("blocked pywinauto")
                return None

        sys.meta_path.insert(0, BlockPywinauto())

        from solin.core.integrations.automation.zoom.controls import ZoomSession

        session = ZoomSession()
        try:
            session.desktop()
        except RuntimeError as exc:
            assert "pywinauto" in str(exc)
        else:
            raise AssertionError("ZoomSession.desktop() should require pywinauto")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_zoom_state_value_objects_are_immutable_and_validated():
    state = MeetingState(
        sharing=ShareState.SHARING,
        participant_count=2,
        participant_names=("Alex", "Sam"),
        in_meeting=True,
    )

    assert state.is_sharing is True
    assert ShareState.from_active(False) is ShareState.NOT_SHARING
    assert ShareState.UNKNOWN.is_active is None
    assert AudioState.DISCONNECTED.is_connected is False
    assert AudioState.UNKNOWN.is_connected is None

    with pytest.raises(FrozenInstanceError):
        state.participant_count = 3  # type: ignore[misc]
    with pytest.raises(ValueError, match="cannot be negative"):
        MeetingState(participant_count=-1)


def test_zoom_i18n_labels_keep_core_actions_and_fallbacks():
    assert "unmute" in AUDIO_MUTED_TEXT
    assert "mute" in AUDIO_UNMUTED_TEXT
    assert "connect audio" in CONNECT_AUDIO_TEXT
    assert "participants" in PARTICIPANTS_TEXT
    assert "start video" in VIDEO_STOPPED_TEXT
    assert "stop video" in VIDEO_STARTED_TEXT
    assert PEOPLE_SEPARATORS[0] == " & "


def test_zoom_toolbar_cache_tracks_live_elements_by_handle():
    class _Rect:
        def width(self):
            return 10

        def height(self):
            return 8

    class _Element:
        def window_text(self):
            return "Mute"

        def rectangle(self):
            return _Rect()

    cache = ToolbarCache()
    element = _Element()

    cache.put(10, "btn_muteAudio", element)

    assert cache.get(None, 10, "btn_muteAudio") is element
    assert cache.get_multi(10, {"btn_muteAudio"}) == {"btn_muteAudio": element}
    assert cache.get_multi(11, {"btn_muteAudio"}) == {}

    cache.invalidate()
    assert cache.get_multi(10, {"btn_muteAudio"}) == {}
    assert "btn_muteAudio" in TOOLBAR_CORE_CONTROL_IDS
    assert "mute_all_btn" in PARTICIPANTS_PANEL_CONTROL_IDS


def test_zoom_text_match_handles_toolbar_suffixes_and_states():
    assert _normalize_toolbar_text("  Mute,\u00a0currently unmuted, Alt+A  ") == (
        "mute, currently unmuted, alt+a"
    )
    assert _matches_toolbar_action("Mute, currently unmuted, Alt+A", ["mute"])
    assert _toolbar_action_match_score("Mute, currently unmuted, Alt+A", ["mute"]) == 4
    assert _audio_button_state_from_text("Mute, currently unmuted, Alt+A") is (
        AudioState.UNMUTED
    )
    assert _audio_button_state_from_text("Unmute, currently muted, Alt+A") is (
        AudioState.MUTED
    )
    assert _video_button_state_from_text("Stop Video, Alt+V") is VideoState.STARTED
    assert _video_button_state_from_text("Start Video, Alt+V") is VideoState.STOPPED


def test_zoom_controls_uses_package_local_zoom_modules():
    source = Path("src/solin/core/integrations/automation/zoom/controls.py").read_text(
        encoding="utf-8"
    )

    assert "sys.exit" not in source
    assert "from .i18n_labels import" in source
    assert "from .toolbar_cache import" in source
    assert "from .text_match import" in source
    assert "from .state import AudioState, MeetingState, ShareState, VideoState" in source


def test_cancelled_zoom_session_rejects_ui_actions():
    session = ZoomSession()
    calls = []
    session.cancel()

    with pytest.raises(RuntimeError, match="no longer active"):
        session.perform_action(lambda: calls.append("invoked"))

    assert calls == []
