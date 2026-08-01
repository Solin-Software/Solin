"""Tests for the Scenes panel widget (edits → config + on_change)."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from solin.core.media.vcam_model import (
    ContentGroup,
    MeetingMode,
    PipCorner,
    VcamSceneConfig,
    preset_for_id,
)
from solin.widgets.scenes_widget import ScenesWidget


def _app():
    if QApplication.instance() is None:  # pragma: no cover - isolated runs
        QApplication([])


def test_reload_reflects_config():
    _app()
    cfg = VcamSceneConfig(mode=MeetingMode.SIGN_LANGUAGE, pip_corner=PipCorner.TOP_RIGHT)
    w = ScenesWidget(cfg, lambda _c: None)
    assert w._mode_combo.currentData() is MeetingMode.SIGN_LANGUAGE
    assert w._corner_combo.currentData() is PipCorner.TOP_RIGHT
    # Sign-language video default = content + camera PiP.
    assert w._rule_combos[ContentGroup.VIDEO].currentData() == "media_with_camera"


def test_mode_change_updates_config_and_emits():
    _app()
    changes: list = []
    cfg = VcamSceneConfig(mode=MeetingMode.REGULAR)
    w = ScenesWidget(cfg, changes.append)

    w._mode_combo.setCurrentIndex(w._mode_combo.findData(MeetingMode.SIGN_LANGUAGE))

    assert cfg.mode is MeetingMode.SIGN_LANGUAGE
    assert changes and changes[-1] is cfg
    # The rule table now reflects the new mode's rules.
    assert w._rule_combos[ContentGroup.VIDEO].currentData() == "media_with_camera"


def test_rule_change_sets_override_and_emits():
    _app()
    changes: list = []
    cfg = VcamSceneConfig(mode=MeetingMode.REGULAR)
    w = ScenesWidget(cfg, changes.append)

    combo = w._rule_combos[ContentGroup.VIDEO]  # default regular = media_full
    combo.setCurrentIndex(combo.findData("media_with_camera"))

    assert cfg.rule(MeetingMode.REGULAR, ContentGroup.VIDEO) == preset_for_id(
        "media_with_camera"
    )
    assert changes


def test_corner_change_updates_config_and_emits():
    _app()
    changes: list = []
    cfg = VcamSceneConfig()
    w = ScenesWidget(cfg, changes.append)

    w._corner_combo.setCurrentIndex(w._corner_combo.findData(PipCorner.TOP_LEFT))

    assert cfg.pip_corner is PipCorner.TOP_LEFT
    assert changes


def test_loading_does_not_emit():
    _app()
    changes: list = []
    cfg = VcamSceneConfig(mode=MeetingMode.SIGN_LANGUAGE)
    # Construction + initial _reload must not fire on_change.
    ScenesWidget(cfg, changes.append)
    assert changes == []
