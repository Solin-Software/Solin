from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tools.translation_editor.main import (
    TranslationGlossaryError,
    TsFile,
    TsFileSaveError,
    _build_ts_system_prompt,
    _load_jw_terminology,
    _placeholder_mismatches,
    _validated_translation,
    run_lupdate,
)


def _write_ts(tmp_path, body: str):
    path = tmp_path / "sample.ts"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TS version="2.1" language="pt_BR">'
        f"{body}"
        "</TS>",
        encoding="utf-8",
    )
    return path


def test_jw_glossary_uses_canonical_application_locales():
    terminology = _load_jw_terminology()
    locale_dir = (
        Path(__file__).parents[2]
        / "src"
        / "solin"
        / "resources"
        / "translations"
        / "locales"
    )
    supported_locales = {
        json.loads(path.read_text(encoding="utf-8"))["meta"]["code"]
        for path in locale_dir.glob("*.json")
    }

    assert set(terminology) == supported_locales - {"en"}
    assert "pt_BR" in terminology
    assert "T" not in terminology
    assert terminology["pt_BR"]["Public Talk"] == "Discurso Público"
    assert all("public_talk" not in terms for terms in terminology.values())
    assert all("Original_Songs" not in terms for terms in terminology.values())
    assert len({frozenset(terms) for terms in terminology.values()}) == 1


def test_system_prompt_looks_up_glossary_by_locale():
    prompt = _build_ts_system_prompt("Português (Brasil)", "pt_BR")

    assert "Português (Brasil) (locale: pt_BR)" in prompt
    assert '"Song" -> "Cântico"' in prompt
    assert "api_code" not in prompt


def test_glossary_loader_rejects_an_unknown_schema(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_text(
        json.dumps({"schema_version": 2, "source_locale": "en", "locales": {}}),
        encoding="utf-8",
    )

    with pytest.raises(TranslationGlossaryError, match="schema_version"):
        _load_jw_terminology(str(path))


def test_ts_file_keeps_context_and_comments_as_distinct_metadata(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>PlaybackControls</name>
          <message>
            <location filename="player.py" line="42" />
            <source>Play</source>
            <comment>verb</comment>
            <extracomment>Button that starts playback.</extracomment>
            <translation>Reproduzir</translation>
          </message>
        </context>
        """,
    )

    entry = TsFile(str(path)).entries[0]

    assert entry.context == "PlaybackControls"
    assert entry.comment == "verb"
    assert entry.extra_comment == "Button that starts playback."
    assert entry.location == "player.py:42"


def test_ts_file_ignores_inactive_messages_and_assigns_unique_ids(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>First</name>
          <message><source>Open</source><translation>Abrir</translation></message>
          <message><source>Old</source><translation type="obsolete">Antigo</translation></message>
        </context>
        <context>
          <name>Second</name>
          <message><source>Open</source><translation>Aberto</translation></message>
          <message><source>Gone</source><translation type="vanished">Sumiu</translation></message>
        </context>
        """,
    )

    entries = TsFile(str(path)).entries

    assert [(entry.context, entry.source) for entry in entries] == [
        ("First", "Open"),
        ("Second", "Open"),
    ]
    assert len({entry.entry_id for entry in entries}) == 2


def test_placeholder_validation_checks_multiplicity_and_each_plural_form(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>Counts</name>
          <message numerus="yes">
            <source>{name}: %n of %1 / %1</source>
            <translation type="unfinished">
              <numerusform>{name}: %n de %1 / %1</numerusform>
              <numerusform>{name}: %n de %1 / %1</numerusform>
            </translation>
          </message>
        </context>
        """,
    )
    entry = TsFile(str(path)).entries[0]

    valid = [
        "{name}: %n de %1 / %1",
        "{name}: %n entre %1 / %1",
    ]
    invalid = [valid[0], "{name}: %n entre %1"]

    assert _validated_translation(entry, valid) == valid
    assert _validated_translation(entry, invalid) is None
    assert _placeholder_mismatches(entry.source, invalid) == [
        "form 2: missing %1"
    ]


def test_plural_validation_preserves_catalog_form_count(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>Counts</name>
          <message numerus="yes">
            <source>%n item(s)</source>
            <translation type="unfinished">
              <numerusform>%n item</numerusform>
              <numerusform>%n itens</numerusform>
            </translation>
          </message>
        </context>
        """,
    )
    entry = TsFile(str(path)).entries[0]

    assert _validated_translation(entry, ["%n item"]) is None
    assert _validated_translation(entry, ["%n item", "%n itens", "%n itens"]) is None


def test_lupdate_extracts_every_percent_n_source_as_numerus(tmp_path):
    project_root = Path(__file__).parents[2]
    source_catalog = (
        project_root
        / "src"
        / "solin"
        / "resources"
        / "translations"
        / "solin_en.ts"
    )
    generated_catalog = tmp_path / source_catalog.name
    shutil.copy2(source_catalog, generated_catalog)
    response_files_before = set(project_root.glob(".solin-lupdate-*.lst"))

    succeeded, output = run_lupdate(
        str(project_root),
        str(generated_catalog),
    )

    assert succeeded, output
    assert set(project_root.glob(".solin-lupdate-*.lst")) == response_files_before
    root = ET.parse(generated_catalog).getroot()
    violations: list[str] = []
    for context in root.findall("context"):
        context_name = context.findtext("name") or ""
        for message in context.findall("message"):
            source = message.findtext("source") or ""
            if "%n" in source and message.get("numerus") != "yes":
                violations.append(f"{context_name}: {source}")

    assert violations == []


def test_ts_save_preserves_inactive_messages(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>Context</name>
          <message><source>Active</source><translation>Ativa</translation></message>
          <message><source>Gone</source><translation type="vanished">Sumiu</translation></message>
        </context>
        """,
    )
    ts_file = TsFile(str(path))

    ts_file.save()

    root = ET.parse(path).getroot()
    messages = root.findall("./context/message")
    assert [message.findtext("source") for message in messages] == ["Active", "Gone"]
    assert messages[1].find("translation").get("type") == "vanished"


def test_ts_noop_save_preserves_catalog_byte_for_byte(tmp_path):
    path = tmp_path / "sample.ts"
    original = (
        "<?xml version='1.0' encoding='utf-8'?>\r\n"
        "<!DOCTYPE TS>\n"
        '<TS version="2.1" language="en">\n'
        "  <context>\n"
        "    <name>Context</name>\n"
        '    <message><source>Open</source><translation type="unfinished"/></message>\n'
        "  </context>\n"
        "</TS>"
    )
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(original)

    TsFile(str(path)).save()

    with open(path, encoding="utf-8", newline="") as stream:
        assert stream.read() == original


def test_ts_save_changes_only_modified_translation(tmp_path):
    path = tmp_path / "sample.ts"
    original = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<!DOCTYPE TS>\n"
        '<TS version="2.1" language="pt_BR">\n'
        "  <context>\n"
        "    <name>Context</name>\n"
        '    <message><source>Open</source><translation type="unfinished"/></message>\n'
        "    <message><source>Close</source><translation>Fechar</translation></message>\n"
        "  </context>\n"
        "</TS>\n"
    )
    path.write_text(original, encoding="utf-8")
    ts_file = TsFile(str(path))
    entry = ts_file.entries[0]
    entry.translation = "Abrir"
    entry.finished = True
    entry.modified = True
    translation = entry._elem.find("translation")
    translation.text = "Abrir"
    translation.attrib.pop("type", None)

    ts_file.save()

    expected = original.replace(
        '<translation type="unfinished"/>',
        "<translation>Abrir</translation>",
        1,
    )
    assert path.read_text(encoding="utf-8") == expected


def test_ts_save_rejects_external_file_changes(tmp_path):
    path = _write_ts(
        tmp_path,
        """
        <context>
          <name>Context</name>
          <message><source>Open</source><translation>Abrir</translation></message>
        </context>
        """,
    )
    ts_file = TsFile(str(path))
    entry = ts_file.entries[0]
    entry.translation = "Abrir arquivo"
    entry.modified = True
    entry._elem.find("translation").text = entry.translation
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(TsFileSaveError, match="Could not save translation file"):
        ts_file.save()
