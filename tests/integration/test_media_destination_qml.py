from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.styles.theme import PALETTE
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.media_destination import (
    DESTINATION_ICON_SVGS,
    MediaDestinationBridge,
    MediaDestinationDialog,
)
from solin.ui.qml.svg_icons import SvgIconProvider


_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip(
        "Media destination QML tests require QApplication.",
        allow_module_level=True,
    )


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    texts: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        texts.append(text)
    for child in item.childItems():
        texts.extend(_visible_texts(child, parent_visible=visible))
    return texts


def test_media_destination_qml_loads_and_follows_bridge_steps() -> None:
    bridge = MediaDestinationBridge(
        media_title="Sample media",
        item_count=1,
        can_play=True,
        playlists=(("one", "Sunday"),),
    )
    widget = QQuickWidget()
    widget.resize(520, 620)
    configure_qml_host(
        widget,
        type_name="MediaDestinationDialog",
        clear_color=PALETTE.bg0,
        context_properties={"destinationBridge": bridge},
        image_providers={
            "destinationicons": SvgIconProvider(
                DESTINATION_ICON_SVGS,
                default_icon="plus",
            )
        },
    )
    widget.show()
    _APP.processEvents()

    root = widget.rootObject()
    assert widget.errors() == []
    assert root is not None
    assert "Play now" in _visible_texts(root)

    bridge.showDestinations()
    bridge.showPlaylists()
    _APP.processEvents()

    visible = _visible_texts(root)
    assert "Choose a playlist" in visible
    assert "Sunday" in visible

    bridge.prepare_meeting_selection(
        monday=bridge.weekMonday,
        pub_type="mwb",
        placement_options=[{"id": "top", "label": "Top", "type": "position", "color": ""}],
    )
    _APP.processEvents()
    assert "Top" in _visible_texts(root)

    widget.close()


def test_native_dialog_uses_compact_step_specific_sizes() -> None:
    bridge = MediaDestinationBridge(
        media_title="Sample",
        item_count=1,
        can_play=True,
        playlists=(),
    )
    dialog = MediaDestinationDialog(bridge)

    assert dialog.size().width() == 460
    assert dialog.size().height() == 390

    bridge.showPlaylists()
    assert dialog.size().height() == 500

    bridge.back()
    bridge.showMeetings()
    assert dialog.size().height() == 440

    dialog.close()


def test_native_dialog_centers_on_its_parent_window() -> None:
    parent = QWidget()
    parent.setGeometry(40, 40, 700, 600)
    parent.show()
    bridge = MediaDestinationBridge(
        media_title="Sample",
        item_count=1,
        can_play=False,
        playlists=(),
    )
    dialog = MediaDestinationDialog(bridge, parent=parent)
    dialog.show()
    QTest.qWait(30)

    parent_center = parent.frameGeometry().center()
    dialog_center = dialog.frameGeometry().center()
    assert abs(parent_center.x() - dialog_center.x()) <= 2
    assert abs(parent_center.y() - dialog_center.y()) <= 2

    dialog.close()
    parent.close()
