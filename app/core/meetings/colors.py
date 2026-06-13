from __future__ import annotations

import colorsys
import random

APP_BASE_HUE = 215
_GOLDEN_ANGLE = 137.508


def _hsl_to_hex(h: int, s: int, l: int) -> str:
    """Convert HSL (h 0-360, s 0-100, l 0-100) to a hex color."""
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def accent_from_hue(hue: int) -> str:
    """Section accent color from hue."""
    return _hsl_to_hex(hue, 55, 52)


def section_text_from_hue(hue: int) -> str:
    """Section title text color from hue."""
    return _hsl_to_hex(hue, 40, 72)


def badge_bg_from_hue(hue: int) -> str:
    """Section count badge background from hue."""
    return _hsl_to_hex(hue, 30, 18)


def card_bg_from_hue(_hue: int) -> str:
    """Section card background."""
    return "#12171f"


def card_border_from_hue(_hue: int) -> str:
    """Section card border."""
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


def section_colors(hue: int) -> dict:
    """Derive a dark-theme section palette from a single hue."""
    return {
        "bg": _hsl_to_hex(hue, 30, 13),
        "bg_hover": _hsl_to_hex(hue, 35, 16),
        "border": _hsl_to_hex(hue, 30, 22),
        "accent": accent_from_hue(hue),
        "text": section_text_from_hue(hue),
        "badge": badge_bg_from_hue(hue),
        "item_bg": _hsl_to_hex(hue, 20, 11),
        "item_bd": _hsl_to_hex(hue, 25, 18),
    }
