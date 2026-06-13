from solin.ui import profile_widgets


def test_profile_widget_styles_live_with_visual_widgets():
    assert profile_widgets._BG == "#0d1117"
    assert "QLineEdit" in profile_widgets._obs_field_style()
    assert "QComboBox" in profile_widgets._obs_combo_style()
    assert "QScrollBar" in profile_widgets._SCROLLBAR_SS
