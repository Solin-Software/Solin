from solin.styles import theme


def test_available_themes_expose_dark_and_light() -> None:
    themes = theme.available_themes()

    assert [item.id for item in themes] == ["dark", "light"]
    assert [item.display_name for item in themes] == ["Dark", "Light"]


def test_light_theme_generates_light_qss_and_qml_tokens() -> None:
    light = theme.get_theme("light")

    assert light.palette.bg0 == "#e8edf3"
    assert light.qml_palette()["bg"] == "#e8edf3"
    assert light.qml_palette()["warningSurface"] == "#eef0e7"
    assert "#e8edf3" in theme.app_stylesheet(light)


def test_activate_theme_updates_public_runtime_tokens() -> None:
    try:
        selected = theme.activate_theme("light")

        assert selected.id == "light"
        assert theme.current_theme().id == "light"
        assert theme.PALETTE.bg0 == "#e8edf3"
        assert dict(theme.QML_THEME)["bg"] == "#e8edf3"
        assert "#e8edf3" in theme.STYLESHEET
    finally:
        theme.activate_theme("dark")


def test_palette_token_resolves_current_theme_after_activation() -> None:
    token = theme.palette_token("bg0")

    try:
        theme.activate_theme("light")
        assert str(token) == "#e8edf3"
        assert f"{token}" == "#e8edf3"

        theme.activate_theme("dark")
        assert token == "#0d1117"
    finally:
        theme.activate_theme("dark")


def test_invalid_theme_ids_fall_back_to_default() -> None:
    assert theme.normalize_theme_id("missing") == theme.DEFAULT_THEME_ID
    assert theme.get_theme("missing").id == theme.DEFAULT_THEME_ID
