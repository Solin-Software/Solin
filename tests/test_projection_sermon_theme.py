from solin.projection.sermon_theme import SermonThemeProjectionWidget, _THEME_BG_SVG


def test_sermon_theme_projection_widget_lives_in_theme_module():
    assert SermonThemeProjectionWidget.__module__ == "solin.projection.sermon_theme"


def test_sermon_theme_svg_stays_embedded_in_python():
    assert _THEME_BG_SVG.startswith("<svg")
    assert "linearGradient" in _THEME_BG_SVG
    assert "viewBox=\"0 0 1024 576\"" in _THEME_BG_SVG
