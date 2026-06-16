from solin.core.meetings.colors import (
    accent_from_hue,
    badge_bg_from_hue,
    card_bg_from_hue,
    card_border_from_hue,
    generate_section_hue,
    hsl_to_hex,
    section_colors,
    section_text_from_hue,
)


def test_section_colors_returns_complete_dark_theme_palette():
    colors = section_colors(215)

    assert set(colors) == {
        "bg",
        "bg_hover",
        "border",
        "accent",
        "text",
        "badge",
        "item_bg",
        "item_bd",
    }
    assert all(value.startswith("#") and len(value) == 7 for value in colors.values())


def test_section_color_helpers_share_the_same_hsl_formula():
    hue = 188

    assert hsl_to_hex(0, 100, 50) == "#ff0000"
    assert accent_from_hue(hue) == section_colors(hue)["accent"]
    assert section_text_from_hue(hue) == section_colors(hue)["text"]
    assert badge_bg_from_hue(hue) == section_colors(hue)["badge"]
    assert card_bg_from_hue(hue) == "#12171f"
    assert card_border_from_hue(hue) == "#1a2030"


def test_generate_section_hue_avoids_existing_hues(monkeypatch):
    monkeypatch.setattr(
        "solin.core.meetings.colors.random.randint",
        lambda _start, _end: 215,
    )

    hue = generate_section_hue([215])
    distance = min(abs(hue - 215), 360 - abs(hue - 215))

    assert distance > 30
