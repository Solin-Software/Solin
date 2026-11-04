from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from solin.widgets.meetings.overview import _PubCard


_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip(
        "Meeting overview tests require QApplication before QCoreApplication.",
        allow_module_level=True,
    )


@pytest.mark.parametrize("card_width", [420, 675, 900])
def test_publication_card_reserves_the_full_wrapped_title_height(
    card_width: int,
) -> None:
    card = _PubCard("wt")
    card.resize(card_width, 148)
    card._title_lbl.setText(
        "Seja sábio em suas decisões sobre ensino adicional"
    )
    card._status_lbl.setText("4 media items")
    card.show()
    _APP.processEvents()

    title = card._title_lbl
    required_height = title.heightForWidth(title.width())
    assert required_height > title.fontMetrics().height()
    assert title.height() >= required_height
    assert card.height() == 148

    card.deleteLater()


def test_publication_card_arrow_stays_centered_for_wrapped_titles() -> None:
    cards = []
    for title in (
        "20-26 de julho",
        "Seja sábio em suas decisões sobre ensino adicional",
    ):
        card = _PubCard("wt")
        card.resize(675, 148)
        card._title_lbl.setText(title)
        card._status_lbl.setText("4 media items")
        card._arrow.setVisible(True)
        card.show()
        cards.append(card)
    _APP.processEvents()

    card_center_y = cards[0].rect().center().y()
    arrow_centers = [
        card._arrow_slot.geometry().center().y()
        for card in cards
    ]
    assert arrow_centers == [card_center_y, card_center_y]
    assert cards[0]._arrow_slot.x() == cards[1]._arrow_slot.x()

    for card in cards:
        card.deleteLater()


def test_manual_maintenance_actions_remain_independent() -> None:
    card = _PubCard("mwb")
    card.set_downloadable()
    card.set_maintenance(update_available=True, repair_required=True)
    events = []
    card.update_requested.connect(lambda: events.append("update"))
    card.repair_requested.connect(lambda: events.append("repair"))
    card.open_requested.connect(lambda: events.append("open"))
    card.show()
    _APP.processEvents()

    QTest.mouseClick(card._repair_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(card._update_button, Qt.MouseButton.LeftButton)

    assert events == ["repair", "update"]
    assert not card._repair_button.isHidden()
    assert not card._update_button.isHidden()
    card.deleteLater()


def test_absent_meeting_can_be_downloaded_from_the_keyboard() -> None:
    card = _PubCard("wt")
    card.set_downloadable()
    events = []
    card.open_requested.connect(lambda: events.append("download"))
    card.show()
    _APP.processEvents()

    QTest.keyClick(card, Qt.Key.Key_Return)

    assert events == ["download"]
    card.set_loading()
    assert not card.isEnabled()
    card.deleteLater()
