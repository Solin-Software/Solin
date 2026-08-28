from __future__ import annotations

import re

from PySide6.QtCore import QCoreApplication, QTranslator

from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.remote_control import (
    REMOTE_CONTROL_TRANSLATION_SOURCES,
    _valid_remote_translation,
    remote_control_localization,
    remote_control_message_sources,
)
from solin.core.i18n.meeting_sections import display_meeting_section_title


_PLACEHOLDER = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")


def test_remote_message_catalog_is_fully_visible_to_lupdate() -> None:
    sources = remote_control_message_sources()

    assert sources
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
        setup_title = QCoreApplication.translate(
            "RemoteControlSetupDialog",
            "Set up Solin Remote",
        )
        sessions_empty = QCoreApplication.translate(
            "RemoteSessionsPopup",
            "No signed-in devices.",
        )
    finally:
        app.removeTranslator(translator)

    assert payload["locale"] == "pt-BR"
    messages = payload["messages"]
    assert isinstance(messages, dict)
    assert messages["app.remoteControl"] == "Controle remoto"
    assert messages["library.chooseMeeting"] == "Escolha uma reunião"
    assert messages["count.media.other"] == "{count} mídias"
    assert messages["setup.title"] == "Configurar o Solin Remoto"
    assert messages["session.disconnectedDevice"].startswith("Este dispositivo")
    assert section_title == "TESOUROS DA PALAVRA DE DEUS"
    assert setup_title == "Configurar o Solin Remoto"
    assert sessions_empty == "Nenhum dispositivo conectado."
    for key, source in remote_control_message_sources().items():
        assert set(_PLACEHOLDER.findall(messages[key])) == set(_PLACEHOLDER.findall(source))


def test_remote_localization_uses_its_dedicated_web_context() -> None:
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


def test_remote_translation_validation_preserves_placeholder_multiplicity() -> None:
    assert _valid_remote_translation(
        "Could not add {title}: {error}",
        "{error}: não foi possível adicionar {title}",
    )
    assert not _valid_remote_translation("{count} items", "{count} de {count} itens")
    assert not _valid_remote_translation("{count} items", "um | alguns | muitos")


def test_remote_localization_rejects_unstructured_plural_variants() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(
        str(application_translation_root() / "solin_ru.qm")
    )
    app.installTranslator(translator)
    try:
        messages = remote_control_localization("ru")["messages"]
    finally:
        app.removeTranslator(translator)

    assert isinstance(messages, dict)
    sources = remote_control_message_sources()
    for key in (
        "count.playlist.other",
        "count.item.other",
        "count.media.other",
        "week.in",
        "week.ago",
    ):
        assert messages[key] == sources[key]
        assert "|" not in messages[key]


def test_every_non_english_catalog_compiles_the_new_remote_control_copy() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translation_root = application_translation_root()
    locale_codes = sorted(
        path.stem for path in (translation_root / "locales").glob("*.json") if path.stem != "en"
    )

    assert len(locale_codes) == 17
    for locale_code in locale_codes:
        translator = QTranslator(app)
        assert translator.load(str(translation_root / f"solin_{locale_code}.qm")), locale_code
        app.installTranslator(translator)
        try:
            install_detail = QCoreApplication.translate(
                "_RemoteControlWeb",
                "Install Solin Remote on this device for quick access from your home screen.",
            )
            password_hint = QCoreApplication.translate(
                "RemoteControlSectionMixin",
                "Use at least %1 characters. Credentials belong only to this profile.",
            )
        finally:
            app.removeTranslator(translator)

        assert install_detail != (
            "Install Solin Remote on this device for quick access from your home screen."
        ), locale_code
        assert password_hint != (
            "Use at least %1 characters. Credentials belong only to this profile."
        ), locale_code
        assert "%1" in password_hint, locale_code
