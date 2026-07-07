from pathlib import Path

from solin.ui import profile_widgets


def test_profile_widget_palette_lives_with_selector_widgets():
    assert profile_widgets.PROFILE_BG == "#0d1117"
    assert profile_widgets.PROFILE_ACCENT
    assert profile_widgets.PROFILE_TEXT


def test_profile_widgets_do_not_export_legacy_onboarding_controls():
    public_api = set(profile_widgets.__all__)

    assert "StepProgress" not in public_api
    assert "OBSToggle" not in public_api
    assert "profile_section_card" not in public_api
    assert "obs_field_style" not in public_api
    assert "obs_combo_style" not in public_api
    assert "PROFILE_SCROLLBAR_STYLESHEET" not in public_api


def test_profile_widgets_keep_profile_selector_scope():
    source = Path("src/solin/ui/profile_widgets.py").read_text(encoding="utf-8")

    assert "class ProfileCard" in source
    assert "class AddProfileCard" in source
    assert "OBSConnectionState" not in source
