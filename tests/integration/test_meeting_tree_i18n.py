from pathlib import Path


def test_meeting_section_dialogs_use_playlist_edit_translation_context():
    source = Path("src/solin/widgets/meetings/tree_controller.py").read_text(
        encoding="utf-8"
    )

    assert '_PLAYLIST_EDIT_CONTEXT = "PlaylistEditView"' in source
    assert '_tr(_PLAYLIST_EDIT_CONTEXT, "Section name:")' in source
    assert '_tr(_PLAYLIST_EDIT_CONTEXT, "New Section")' in source
    assert '_tr("_PlaylistEditView", "Section name:")' not in source
    assert '_tr("_PlaylistEditView", "New Section")' not in source
