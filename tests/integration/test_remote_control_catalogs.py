from __future__ import annotations

import re

from PySide6.QtCore import QCoreApplication, QTranslator

from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.meeting_sections import display_meeting_section_title
from solin.core.i18n.remote_control import (
    remote_media_localization,
    remote_media_message_sources,
    remote_timer_localization,
    remote_timer_message_sources,
)
from tests._translation_catalog import catalog_translation


_PLACEHOLDER = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")


def test_remote_localization_uses_active_qt_catalog_and_preserves_placeholders(
    compiled_translation_catalogs,
) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(str(compiled_translation_catalogs / "solin_pt_BR.qm"))
    assert app.installTranslator(translator)
    try:
        media_payload = remote_media_localization("pt_BR")
        timer_payload = remote_timer_localization("pt_BR")
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
            "Set up Solin Media",
        )
        timer_setup_title = QCoreApplication.translate(
            "RemoteControlSetupDialog",
            "Set up Solin Timer",
        )
        sessions_empty = QCoreApplication.translate(
            "RemoteSessionsPopup",
            "No signed-in devices.",
        )
    finally:
        app.removeTranslator(translator)

    assert media_payload["locale"] == "pt-BR"
    assert timer_payload["locale"] == "pt-BR"
    media_messages = media_payload["messages"]
    timer_messages = timer_payload["messages"]
    assert isinstance(media_messages, dict)
    assert isinstance(timer_messages, dict)

    media_sources = remote_media_message_sources()
    for key in (
        "app.title",
        "app.mediaControl",
        "library.chooseMeeting",
        "count.media.other",
        "setup.title",
        "session.disconnectedDevice",
    ):
        assert media_messages[key] == catalog_translation(
            "pt_BR", "_RemoteControlWeb", media_sources[key]
        )

    timer_sources = remote_timer_message_sources()
    for key in (
        "app.title",
        "stage.controlParts",
        "stage.expand",
        "stage.expandTimer",
        "setup.title",
        "part.start",
        "session.disconnectedDevice",
    ):
        assert timer_messages[key] == catalog_translation(
            "pt_BR", "_RemoteControlWeb", timer_sources[key]
        )

    assert section_title == catalog_translation(
        "pt_BR", "_Section", "TREASURES FROM GOD'S WORD"
    )
    assert setup_title == catalog_translation(
        "pt_BR", "RemoteControlSetupDialog", "Set up Solin Media"
    )
    assert timer_setup_title == catalog_translation(
        "pt_BR", "RemoteControlSetupDialog", "Set up Solin Timer"
    )
    assert sessions_empty == catalog_translation(
        "pt_BR", "RemoteSessionsPopup", "No signed-in devices."
    )
    for key, source in media_sources.items():
        assert set(_PLACEHOLDER.findall(media_messages[key])) == set(
            _PLACEHOLDER.findall(source)
        )
    for key, source in timer_sources.items():
        assert set(_PLACEHOLDER.findall(timer_messages[key])) == set(
            _PLACEHOLDER.findall(source)
        )


def test_remote_localization_uses_its_dedicated_web_context(
    compiled_translation_catalogs,
) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(str(compiled_translation_catalogs / "solin_es.qm"))
    assert app.installTranslator(translator)
    try:
        media_messages = remote_media_localization("es")["messages"]
        timer_messages = remote_timer_localization("es")["messages"]
    finally:
        app.removeTranslator(translator)

    assert isinstance(media_messages, dict)
    assert isinstance(timer_messages, dict)
    media_sources = remote_media_message_sources()
    timer_sources = remote_timer_message_sources()
    for key in ("nav.meetings", "library.chooseMeeting"):
        assert media_messages[key] == catalog_translation(
            "es", "_RemoteControlWeb", media_sources[key]
        )
    for key in ("meeting.midweek", "stage.controlParts"):
        assert timer_messages[key] == catalog_translation(
            "es", "_RemoteControlWeb", timer_sources[key]
        )


def test_remote_localization_rejects_unstructured_plural_variants(
    compiled_translation_catalogs,
) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator(app)
    assert translator.load(str(compiled_translation_catalogs / "solin_ru.qm"))
    assert app.installTranslator(translator)
    try:
        messages = remote_media_localization("ru")["messages"]
    finally:
        app.removeTranslator(translator)

    assert isinstance(messages, dict)
    sources = remote_media_message_sources()
    for key in (
        "count.playlist.other",
        "count.item.other",
        "count.media.other",
        "week.in",
        "week.ago",
    ):
        assert messages[key] == sources[key]
        assert "|" not in messages[key]


def test_every_non_english_catalog_compiles_the_new_remote_control_copy(
    compiled_translation_catalogs,
) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translation_root = application_translation_root()
    locale_codes = sorted(
        path.stem
        for path in (translation_root / "locales").glob("*.json")
        if path.stem != "en"
    )

    assert len(locale_codes) == 17
    for locale_code in locale_codes:
        translator = QTranslator(app)
        assert translator.load(
            str(compiled_translation_catalogs / f"solin_{locale_code}.qm")
        ), locale_code
        assert app.installTranslator(translator)
        try:
            install_detail = QCoreApplication.translate(
                "_RemoteControlWeb",
                "Install Solin Media on this device for quick access from your home screen.",
            )
            timer_install_detail = QCoreApplication.translate(
                "_RemoteControlWeb",
                "Install Solin Timer on this device for standalone fullscreen access from your home screen.",
            )
            password_hint = QCoreApplication.translate(
                "SettingsWidget",
                "Use at least %1 characters. Credentials belong only to this profile.",
            )
        finally:
            app.removeTranslator(translator)

        assert install_detail == catalog_translation(
            locale_code,
            "_RemoteControlWeb",
            "Install Solin Media on this device for quick access from your home screen.",
        ), locale_code
        assert timer_install_detail == catalog_translation(
            locale_code,
            "_RemoteControlWeb",
            "Install Solin Timer on this device for standalone fullscreen access from your home screen.",
        ), locale_code
        assert password_hint == catalog_translation(
            locale_code,
            "SettingsWidget",
            "Use at least %1 characters. Credentials belong only to this profile.",
        ), locale_code
        assert "%1" in password_hint, locale_code


def test_timer_editor_messages_resolve_in_the_remote_context(compiled_translation_catalogs):
    sources = remote_timer_message_sources()
    editor_keys = (
        "schedule.emptySection",
        "workspace.editSchedule",
        "pacing.setTime",
        "pacing.configureInSolin",
        "scheduleEditor.sectionPreview",
        "scheduleEditor.customHex",
        "scheduleEditor.advanced",
        "scheduleEditor.adjustableParts",
    )
    for catalog in compiled_translation_catalogs.glob("solin_*.qm"):
        translator = QTranslator()
        assert translator.load(str(catalog)), catalog.name
        for key in editor_keys:
            assert translator.translate("_RemoteControlWeb", sources[key]).strip(), (
                catalog.name,
                key,
            )
