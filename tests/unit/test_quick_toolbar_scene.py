"""Tests for the virtual-camera scene-override surface on the quick toolbar bridge."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from solin.ui.qml.quick_toolbar import QUICK_TOOLBAR_ICON_SVGS, QuickToolbarBridge


def _app():
    if QApplication.instance() is None:  # pragma: no cover - isolated runs
        QApplication([])


def test_scene_icon_registered():
    assert "scene" in QUICK_TOOLBAR_ICON_SVGS


def test_scene_visibility_and_color_properties():
    _app()
    bridge = QuickToolbarBridge()
    assert bridge.sceneVisible is False  # hidden until the vcam is running
    bridge.set_scene_visible(True)
    assert bridge.sceneVisible is True
    bridge.set_scene_icon_color("#8b949e")
    assert bridge.sceneIconColor == "8b949e"  # '#' stripped


def test_on_scene_clicked_emits_signal():
    _app()
    bridge = QuickToolbarBridge()
    fired: list[bool] = []
    bridge.sceneClicked.connect(lambda: fired.append(True))
    bridge.onSceneClicked()
    assert fired == [True]


def test_scene_tooltip_is_translated_nonempty():
    _app()
    bridge = QuickToolbarBridge()
    assert bridge.sceneTooltip  # set by update_translations()
