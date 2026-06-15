from solin.ui import profile_widgets


def test_profile_widget_styles_live_with_visual_widgets():
    assert profile_widgets.PROFILE_BG == "#0d1117"
    assert "QLineEdit" in profile_widgets.obs_field_style()
    assert "QComboBox" in profile_widgets.obs_combo_style()
    assert "QScrollBar" in profile_widgets.PROFILE_SCROLLBAR_STYLESHEET
