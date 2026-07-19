from pathlib import Path
import xml.etree.ElementTree as ET


def test_meeting_section_dialogs_use_playlist_edit_translation_context():
    source = Path("src/solin/widgets/meetings/tree_controller.py").read_text(
        encoding="utf-8"
    )

    assert '_PLAYLIST_EDIT_CONTEXT = "PlaylistEditView"' in source
    assert '_tr(_PLAYLIST_EDIT_CONTEXT, "Section name:")' in source
    assert '_tr(_PLAYLIST_EDIT_CONTEXT, "New Section")' in source
    assert '_tr("_PlaylistEditView", "Section name:")' not in source
    assert '_tr("_PlaylistEditView", "New Section")' not in source


def test_canonical_restore_catalog_is_complete_in_brazilian_portuguese():
    catalog = ET.parse(
        "src/solin/resources/translations/solin_pt_BR.ts"
    ).getroot()
    contexts = {
        context.findtext("name"): context
        for context in catalog.findall("context")
    }
    restore = contexts["MeetingCanonicalRestore"]
    translations = {
        message.findtext("source"): message.findtext("translation")
        for message in restore.findall("message")
    }

    assert translations["Restore official meeting content"] == (
        "Restaurar conteúdo oficial da reunião"
    )
    confirmation = (
        "Restore the official meeting content?\n\n"
        "Manually added content, trims, framing and expanded state will be kept."
    )
    assert translations[confirmation] == (
        "Restaurar o conteúdo oficial da reunião?\n\n"
        "O conteúdo adicionado manualmente, os cortes, o enquadramento e o estado "
        "de expansão serão mantidos."
    )
    assert all("%1" not in source for source in translations)
    assert all(translations.values())
