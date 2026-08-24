from solin.styles import theme


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


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


def test_secondary_controls_keep_non_text_contrast_across_themes() -> None:
    for theme_id in ("dark", "light"):
        palette = theme.get_theme(theme_id).qml_palette()

        for surface_token in ("surface", "surfaceAlt"):
            assert _contrast_ratio(
                palette["secondaryControl"], palette[surface_token]
            ) >= 3.0

        assert _contrast_ratio(
            palette["secondaryControlHover"], palette["hover"]
        ) >= 3.0
