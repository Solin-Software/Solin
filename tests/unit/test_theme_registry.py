from solin.styles import theme


def test_available_themes_expose_dark_and_light() -> None:
    themes = theme.available_themes()

    assert [item.id for item in themes] == ["dark", "light"]
    assert [item.display_name for item in themes] == ["Dark", "Light"]


def test_light_theme_generates_light_qss_and_qml_tokens() -> None:
    light = theme.get_theme("light")

    assert light.palette.bg0 == "#f6f8fa"
    assert light.qml_palette()["bg"] == "#f6f8fa"
    assert light.qml_palette()["hoverOpacity"] < 1.0
    assert light.qml_palette()["sectionHoverOpacity"] < 1.0
    assert "#f6f8fa" in theme.app_stylesheet(light)


def test_activate_theme_updates_public_runtime_tokens() -> None:
    try:
        selected = theme.activate_theme("light")

        assert selected.id == "light"
        assert theme.current_theme().id == "light"
        assert theme.PALETTE.bg0 == "#f6f8fa"
        assert dict(theme.QML_THEME)["bg"] == "#f6f8fa"
        assert "#f6f8fa" in theme.STYLESHEET
    finally:
        theme.activate_theme("dark")


def test_invalid_theme_ids_fall_back_to_default() -> None:
    assert theme.normalize_theme_id("missing") == theme.DEFAULT_THEME_ID
    assert theme.get_theme("missing").id == theme.DEFAULT_THEME_ID
