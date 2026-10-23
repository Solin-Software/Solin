from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QTranslator

from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.media_placement import translate_media_placement
from tests._translation_catalog import catalog_translation


def test_all_declared_languages_have_loadable_catalogs(compiled_translation_catalogs: Path) -> None:
    metadata_directory = application_translation_root() / "locales"
    expected = {f"solin_{path.stem}.qm" for path in metadata_directory.glob("*.json")}
    catalogs = sorted(compiled_translation_catalogs.glob("solin_*.qm"))

    assert len(expected) == 18
    assert {path.name for path in catalogs} == expected
    for catalog in catalogs:
        translator = QTranslator()
        assert translator.load(str(catalog)), catalog.name
        assert not translator.isEmpty(), catalog.name


@pytest.mark.parametrize(
    ("count", "plural_form"),
    [(0, 1), (1, 0), (2, 1)],
)
def test_english_catalog_resolves_qt_plural_forms(
    compiled_translation_catalogs: Path, count: int, plural_form: int,
) -> None:
    source = "%n NDI source found."
    expected = catalog_translation(
        "en", "IntegrationSettings", source, plural_form=plural_form,
    ).replace("%n", str(count))
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator()
    assert translator.load(str(compiled_translation_catalogs / "solin_en.qm"))
    assert app.installTranslator(translator)
    try:
        assert QCoreApplication.translate(
            "IntegrationSettings", "%n NDI source found.", None, count,
        ) == expected
    finally:
        app.removeTranslator(translator)


@pytest.mark.parametrize(
    ("context", "source", "disambiguation"),
    [
        ("ScenesBridge", "Waiting for camera frames…", None),
        (
            "ScenesBridge",
            "Camera capabilities are unavailable. You can try automatic mode.",
            None,
        ),
        ("ScenesEditorView", "Resolution", None),
        ("TimerView", "Timer", None),
        ("TimerView", "Timer", "countdown mode"),
        ("SceneOutputStatus", "On", None),
        (
            "SceneOutputStatus",
            "The virtual camera failed. Turn it off and on to try again.",
            None,
        ),
    ],
)
def test_runtime_catalog_preserves_current_messages_context_and_disambiguation(
    compiled_translation_catalogs: Path,
    context: str,
    source: str,
    disambiguation: str | None,
) -> None:
    expected = catalog_translation("pt_BR", context, source, disambiguation)
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator()
    assert translator.load(str(compiled_translation_catalogs / "solin_pt_BR.qm"))
    assert app.installTranslator(translator)
    try:
        assert QCoreApplication.translate(context, source, disambiguation) == expected
    finally:
        app.removeTranslator(translator)


def test_shared_placement_and_trim_contexts_resolve_from_runtime_catalog(compiled_translation_catalogs) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    translator = QTranslator()
    assert translator.load(
        str(compiled_translation_catalogs / "solin_pt_BR.qm")
    )
    assert app.installTranslator(translator)
    try:
        for source in ("Top of playlist", "End of playlist"):
            assert translate_media_placement(source) == catalog_translation("pt_BR", "MediaPlacement", source)
        assert (
            QCoreApplication.translate("MediaTrimDialog", "Start and end times")
            == catalog_translation("pt_BR", "MediaTrimDialog", "Start and end times")
        )
    finally:
        app.removeTranslator(translator)
