from PySide6.QtCore import QCoreApplication

from solin.core.i18n.strings import (
    tr_document_page_title,
    tr_item_count,
    tr_jw_playlist_title,
    tr_jw_playlist_unresolved,
    tr_media_item_count,
)


def test_item_count_uses_established_singular_and_plural_forms():
    QCoreApplication.instance() or QCoreApplication([])

    assert tr_item_count(0) == "0 item(s)"
    assert tr_item_count(1) == "1 item(s)"
    assert tr_item_count(163) == "163 item(s)"


def test_media_item_count_uses_established_numerus_forms():
    QCoreApplication.instance() or QCoreApplication([])

    assert tr_media_item_count(0) == "0 media item(s)"
    assert tr_media_item_count(1) == "1 media item(s)"
    assert tr_media_item_count(163) == "163 media item(s)"


def test_jw_playlist_feedback_keeps_dynamic_details_inside_the_message():
    QCoreApplication.instance() or QCoreApplication([])

    assert tr_jw_playlist_title() == "JW Library Playlist"
    assert tr_jw_playlist_unresolved("• Song 1\n• Song 2") == (
        "Some items could not be resolved. Check the internet connection:\n\n"
        "• Song 1\n• Song 2"
    )


def test_document_page_title_is_a_complete_reorderable_message():
    QCoreApplication.instance() or QCoreApplication([])

    assert tr_document_page_title("Slides", 3) == "Slides — page 3"
    assert tr_document_page_title("Slides", 0) == "Slides — page 1"
