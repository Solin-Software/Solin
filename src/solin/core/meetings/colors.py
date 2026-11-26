from __future__ import annotations

import colorsys
import random
from typing import Literal

APP_BASE_HUE = 215
_GOLDEN_ANGLE = 137.508
ThemeScheme = Literal["dark", "light"]


def hsl_to_hex(h: int, s: int, l: int) -> str:
    """Convert HSL (h 0-360, s 0-100, l 0-100) to a hex color."""
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def accent_from_hue(hue: int) -> str:
    """Section accent color from hue."""
    return hsl_to_hex(hue, 55, 52)


def _is_light(scheme: ThemeScheme) -> bool:
    return scheme == "light"


def section_text_from_hue(hue: int, scheme: ThemeScheme = "dark") -> str:
    """Section title text color from hue."""
    if _is_light(scheme):
        return hsl_to_hex(hue, 58, 34)
    return hsl_to_hex(hue, 40, 72)


def badge_bg_from_hue(hue: int, scheme: ThemeScheme = "dark") -> str:
    """Section count badge background from hue."""
    if _is_light(scheme):
        return hsl_to_hex(hue, 55, 92)
    return hsl_to_hex(hue, 30, 18)


def card_bg_from_hue(hue: int, scheme: ThemeScheme = "dark") -> str:
    """Section card background."""
    if _is_light(scheme):
        return hsl_to_hex(hue, 35, 97)
    return "#12171f"


def card_border_from_hue(hue: int, scheme: ThemeScheme = "dark") -> str:
    """Section card border."""
    if _is_light(scheme):
        return hsl_to_hex(hue, 35, 86)
    return "#1a2030"


def generate_section_hue(existing_hues: list[int]) -> int:
    """Pick a new hue that is harmonious with the app theme and existing sections."""
    base_offset = random.randint(0, 359)
    for attempt in range(72):
        candidate = int((base_offset + attempt * _GOLDEN_ANGLE) % 360)
        if all(
            min(abs(candidate - h), 360 - abs(candidate - h)) > 30
            for h in existing_hues
        ):
            return candidate
    return random.randint(0, 359)


def section_colors(hue: int, scheme: ThemeScheme = "dark") -> dict:
    """Derive a theme-aware section palette from a single hue."""
    if _is_light(scheme):
        return {
            "bg": hsl_to_hex(hue, 35, 97),
            "bg_hover": hsl_to_hex(hue, 42, 94),
            "border": hsl_to_hex(hue, 35, 84),
            "accent": accent_from_hue(hue),
            "text": section_text_from_hue(hue, scheme),
            "badge": badge_bg_from_hue(hue, scheme),
            "item_bg": hsl_to_hex(hue, 30, 98),
            "item_bd": hsl_to_hex(hue, 35, 88),
        }
    return {
        "bg": hsl_to_hex(hue, 30, 13),
        "bg_hover": hsl_to_hex(hue, 35, 16),
        "border": hsl_to_hex(hue, 30, 22),
        "accent": accent_from_hue(hue),
        "text": section_text_from_hue(hue, scheme),
        "badge": badge_bg_from_hue(hue, scheme),
        "item_bg": hsl_to_hex(hue, 20, 11),
        "item_bd": hsl_to_hex(hue, 25, 18),
    }


def section_colors_from_hex(hex_color: str, scheme: ThemeScheme = "dark") -> dict[str, str]:
    """Derive a theme-aware section palette from any ``#rrggbb`` color.

    Keeps the chosen color recognizable for icon/text while generating a
    subtle darker (dark theme) or tinted light (light theme) badge background,
    matching the visual structure of ``section_colors(hue, scheme)``.
    """
    cleaned = hex_color.strip().lower()
    r = int(cleaned[1:3], 16) / 255.0
    g = int(cleaned[3:5], 16) / 255.0
    b = int(cleaned[5:7], 16) / 255.0
    h, l_val, s_val = colorsys.rgb_to_hls(r, g, b)
    hue = int(round(h * 360.0)) % 360
    sat_pct = int(round(s_val * 100.0))
    light_pct = int(round(l_val * 100.0))

    if sat_pct < 8:
        if _is_light(scheme):
            return {
                "accent": cleaned,
                "text": hsl_to_hex(0, 0, min(light_pct, 36)),
                "badge": hsl_to_hex(0, 0, 92),
                "border": hsl_to_hex(0, 0, 84),
            }
        return {
            "accent": cleaned,
            "text": hsl_to_hex(0, 0, max(light_pct, 68)),
            "badge": hsl_to_hex(0, 0, 18),
            "border": hsl_to_hex(0, 0, 25),
        }

    if _is_light(scheme):
        text_color = cleaned if light_pct <= 42 else hsl_to_hex(hue, min(sat_pct, 64), 34)
        return {
            "accent": cleaned,
            "text": text_color,
            "badge": hsl_to_hex(hue, min(sat_pct, 55), 92),
            "border": hsl_to_hex(hue, min(sat_pct, 38), 84),
        }

    text_color = cleaned if light_pct >= 56 else hsl_to_hex(hue, min(sat_pct, 58), 68)
    return {
        "accent": cleaned,
        "text": text_color,
        "badge": hsl_to_hex(hue, min(sat_pct, 36), 18),
        "border": hsl_to_hex(hue, min(sat_pct, 34), 24),
    }
