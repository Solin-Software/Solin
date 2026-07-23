from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

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
