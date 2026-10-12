from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget

from solin.widgets.obs_scene_popup import OBSScenePopup, _SceneChipButton


@pytest.fixture
def popup():
    owner = QWidget()
    scene_popup = OBSScenePopup(owner)
    try:
        yield scene_popup
    finally:
        scene_popup.close()
        owner.deleteLater()


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("active_first", [False, True])
def test_ndi_button_tracks_availability_and_activity(popup, available, active, active_first):
    updates = (
        (popup.set_stream_available, available),
        (popup.set_stream_active, active),
    )
    for setter, value in reversed(updates) if active_first else updates:
        setter(value)

    button = popup._stream_btn
    assert button.isHidden() == (not (available or active))
    assert button.text() == popup.tr("Stop Stream" if active else "Show Stream")
    assert button.toolTip() == popup.tr(
        "Stop OBS program stream" if active else "Project OBS program stream"
    )
    if available or active:
        requests = []
        popup.stream_requested.connect(lambda: requests.append(True))
        button.click()
        assert requests == [True]


def test_ndi_stop_remains_available_when_configuration_is_removed(popup):
    popup.set_stream_available(True)
    popup.set_stream_active(True)
    popup.set_stream_available(False)
    popup.apply_theme()

    assert not popup._stream_btn.isHidden()
    assert popup._stream_btn.text() == popup.tr("Stop Stream")

    popup.set_stream_active(False)
    assert popup._stream_btn.isHidden()


@pytest.mark.parametrize(
    ("current", "media", "can_return"),
    [("Media", "Media", True), ("Camera", "Media", False), ("Media", "", False)],
)
def test_scene_chips_keep_camera_scenes_and_independent_return_selection(
    popup, current, media, can_return
):
    selections = []
    returns = []
    popup.scene_change_requested.connect(selections.append)
    popup.return_scene_requested.connect(returns.append)
    popup.populate(["Camera", "Media", "Lectern"], current, "Camera", media)
    chips = {chip.text(): chip for chip in popup.findChildren(_SceneChipButton)}
    assert set(chips) == {"Camera", "Media", "Lectern"}

    chips["Camera"].click()
    assert selections == ["Camera"]
    assert returns == []

    chips["Lectern"].right_clicked.emit()
    assert returns == (["Lectern"] if can_return else [])
    assert selections == ["Camera"]

    chips[current].right_clicked.emit()
    assert returns == (["Lectern"] if can_return else [])


def test_empty_scene_list_does_not_disable_ndi_controls(popup):
    popup.set_stream_available(True)
    popup.populate([], "", "", "")
    assert not popup.findChildren(_SceneChipButton)
    assert not popup._stream_btn.isHidden()
