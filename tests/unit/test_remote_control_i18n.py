from __future__ import annotations

import re

from PySide6.QtCore import QCoreApplication, QTranslator

from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.remote_control import (
    REMOTE_CONTROL_TRANSLATION_SOURCES,
    remote_control_localization,
    remote_control_message_sources,
)
from solin.core.i18n.meeting_sections import display_meeting_section_title


_PLACEHOLDER = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")


def test_remote_message_catalog_is_fully_visible_to_lupdate() -> None:
    sources = remote_control_message_sources()

    assert len(sources) == 140
    assert set(sources.values()) == set(REMOTE_CONTROL_TRANSLATION_SOURCES)


def test_remote_localization_uses_active_qt_catalog_and_preserves_placeholders() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(str(application_translation_root() / "solin_pt_BR.qm"))
    app.installTranslator(translator)
    try:
        payload = remote_control_localization("pt_BR")
        section_title = display_meeting_section_title(
            {
                "type": "section",
                "title": "TREASURES FROM GOD'S WORD",
                "meeting_generated": True,
                "section_code": "tgw",
            }
        )
    finally:
        app.removeTranslator(translator)

    assert payload["locale"] == "pt-BR"
    messages = payload["messages"]
    assert isinstance(messages, dict)
    assert messages["app.remoteControl"] == "Controle remoto"
    assert messages["library.chooseMeeting"] == "Escolha uma reunião"
    assert messages["count.media.other"] == "{count} mídias"
    assert section_title == "TESOUROS DA PALAVRA DE DEUS"
    for key, source in remote_control_message_sources().items():
        assert set(_PLACEHOLDER.findall(messages[key])) == set(_PLACEHOLDER.findall(source))


def test_remote_localization_reuses_existing_solin_vocabulary() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(str(application_translation_root() / "solin_es.qm"))
    app.installTranslator(translator)
    try:
        messages = remote_control_localization("es")["messages"]
    finally:
        app.removeTranslator(translator)

    assert isinstance(messages, dict)
    assert messages["nav.meetings"] == "Reuniones"
    assert messages["library.chooseMeeting"] == "Elegir una reunión"
