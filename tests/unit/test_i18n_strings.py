from PySide6.QtCore import QCoreApplication

from solin.core.i18n.strings import tr_item_count


def test_item_count_uses_established_singular_and_plural_forms():
    QCoreApplication.instance() or QCoreApplication([])

    assert tr_item_count(0) == "0 items"
    assert tr_item_count(1) == "1 item"
    assert tr_item_count(163) == "163 items"
