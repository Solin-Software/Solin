"""Single source of truth for Solin's UI theme.

Keep product-level colors here and import tokens from consumers. Component
styles may still live with their widgets, but the palette, state colors, and
shared stylesheets should come from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ThemePalette:
    """Semantic color tokens for the current Solin dark theme."""

    bg0: str = "#0d1117"
    bg1: str = "#161b22"
    bg2: str = "#21262d"
    bg3: str = "#2d333b"

    surface: str = "#161b22"
    surface_card: str = "#13161c"
    surface_alt: str = "#0f131a"
    surface_input_focus: str = "#0e1720"
    surface_hover: str = "#1a1f2a"
    surface_hover_strong: str = "#1c2128"
    surface_overlay: str = "#11161d"
    media_placeholder: str = "#0a0e14"

    border: str = "#30363d"
    border_muted: str = "#21262d"
    border_subtle: str = "#1e2430"
    border_strong: str = "#2a3040"

    text_primary: str = "#e6edf3"
    text_secondary: str = "#c9d1d9"
    text_muted: str = "#8b949e"
    text_dim: str = "#484f58"
    text_faint: str = "#6e7681"
    text_on_accent: str = "#ffffff"

    accent: str = "#388bfd"
    accent_alt: str = "#4f8cc9"
    accent_hover: str = "#58a6ff"
    accent_pressed: str = "#2f7be0"
    accent_selection: str = "#1f6feb"
    accent_muted: str = "#1f3a5f"
    accent_muted_hover: str = "#2a4f7f"
    accent_tint: str = "#132a46"
    accent_text: str = "#79c0ff"
    accent_text_hover: str = "#cae8ff"

    success: str = "#3fb950"
    success_surface: str = "#182518"
    success_surface_strong: str = "#0f2d16"
    success_border: str = "#1a5c2a"
    success_hover: str = "#46b556"
    success_pressed: str = "#1a6e2a"
    warning: str = "#d29922"
    warning_text: str = "#e3b341"
    danger: str = "#f85149"
    danger_text: str = "#ff7b72"
    danger_surface: str = "#2d1b1b"
    danger_border: str = "#3d2020"
    danger_surface_hover: str = "#3d2424"
    danger_subtle: str = "#3d1214"

    projection: str = "#2ea043"
    titlebar: str = "#1A231F"
    black: str = "#000000"
    white: str = "#ffffff"

    def as_qml(self) -> dict[str, str]:
        """Return color tokens in the names used by QML views."""

        return {
            "bg": self.bg0,
            "surface": self.surface_card,
            "surfaceChrome": self.surface,
            "surfaceAlt": self.surface_alt,
            "surfaceInputFocus": self.surface_input_focus,
            "surface2": self.surface_overlay,
            "hover": self.surface_hover,
            "hoverStrong": self.surface_hover_strong,
            "hoverBorder": self.border_strong,
            "border": self.border_muted,
            "border_": self.border_subtle,
            "borderChrome": self.border,
            "borderStrong": self.border_strong,
            "textPrimary": self.text_primary,
            "textSecondary": self.text_secondary,
            "textMuted": self.text_muted,
            "textDim": self.text_dim,
            "textFaint": self.text_faint,
            "textOnAccent": self.text_on_accent,
            "accent": self.accent,
            "accentHover": self.accent_hover,
            "accentPressed": self.accent_pressed,
            "accentSelection": self.accent_selection,
            "accentMuted": self.accent_muted,
            "accentMutedHover": self.accent_muted_hover,
            "accentTint": self.accent_tint,
            "accentText": self.accent_text,
            "accentTextHover": self.accent_text_hover,
            "success": self.success,
            "successSurface": self.success_surface,
            "successSurfaceStrong": self.success_surface_strong,
            "successBorder": self.success_border,
            "warning": self.warning,
            "amber": self.warning,
            "danger": self.danger,
            "dangerText": self.danger_text,
            "dangerSubtle": self.danger_subtle,
            "white": self.white,
            "black": self.black,
            "mediaPlaceholder": self.media_placeholder,
        }


@dataclass(frozen=True)
class ThemeMetrics:
    sidebar_w: str = "64px"
    sidebar_expanded_w: str = "220px"


@dataclass(frozen=True)
class AppTheme:
    palette: ThemePalette = ThemePalette()
    metrics: ThemeMetrics = ThemeMetrics()

    def qml_palette(self) -> dict[str, str]:
        return self.palette.as_qml()


THEME = AppTheme()
APP_THEME = THEME
PALETTE = THEME.palette
METRICS = THEME.metrics
QML_THEME = MappingProxyType(THEME.qml_palette())


def qss_rgba(hex_color: str, alpha: float) -> str:
    """Return a Qt stylesheet rgba() color derived from a hex token."""

    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB color, got {hex_color!r}")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:g})"


def scrollbar_stylesheet() -> str:
    p = PALETTE
    return f"""
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: {p.bg1};
    width: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {p.bg3};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_muted};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{
    background: {p.bg1};
    height: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {p.bg3};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


def app_stylesheet() -> str:
    p = PALETTE
    return f"""
/* === Global === */
QWidget {{
    background-color: {p.bg0};
    color: {p.text_primary};
    font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    border: none;
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {p.bg0};
}}

/* === Scrollbars === */
{scrollbar_stylesheet()}

/* === Sidebar === */
#Sidebar {{
    background-color: {p.bg1};
    border-right: 1px solid {p.border};
}}

#SidebarToggleBtn {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0;
}}
#SidebarToggleBtn:hover {{
    background-color: {p.bg2};
}}

#SidebarBtn {{
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 10px;
    color: {p.text_muted};
    font-size: 12px;
    text-align: left;
}}
#SidebarBtn:hover {{
    background-color: {p.bg2};
    color: {p.text_primary};
}}
#SidebarBtn[active="true"] {{
    background-color: {p.accent_muted};
    color: {p.accent};
}}
#SidebarBtn[compact="true"] {{
    padding: 10px 12px;
    text-align: center;
}}

#SidebarLangBtn {{
    background: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px 10px;
    color: {p.text_muted};
    font-size: 11px;
    font-weight: 600;
}}
#SidebarLangBtn:hover {{
    background: {p.bg3};
    color: {p.text_primary};
}}

/* === Content Area === */
#ContentArea {{
    background-color: {p.bg0};
}}

/* === Cards === */
#Card {{
    background-color: {p.bg1};
    border: 1px solid {p.border};
    border-radius: 12px;
}}

/* === Section Headers === */
#SectionTitle {{
    background: transparent;
    font-size: 20px;
    font-weight: 700;
    color: {p.text_primary};
}}
#SectionSubtitle {{
    background: transparent;
    font-size: 12px;
    color: {p.text_muted};
}}

/* === Search Bar === */
#SearchBar {{
    background-color: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 8px 14px;
    color: {p.text_primary};
    font-size: 13px;
    selection-background-color: {p.accent_muted};
}}
#SearchBar:focus {{
    border-color: {p.accent_alt};
}}
#SearchBar::placeholder {{
    color: {p.text_dim};
}}

/* === Song List === */
#SongList {{
    background-color: transparent;
    border: none;
    outline: none;
}}
#SongList::item {{
    background: {p.bg1};
    border: 1px solid {p.border_muted};
    border-radius: 8px;
    padding: 0px;
    margin: 2px 0;
    color: {p.text_primary};
}}
#SongList::item:hover {{
    background: {p.bg2};
    border-color: {p.border};
}}
#SongList::item:selected {{
    background: {p.accent_muted};
    border-color: {p.accent_alt};
    color: {p.text_primary};
}}

/* === Buttons === */
QPushButton {{
    background-color: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 8px 18px;
    color: {p.text_primary};
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {p.bg3};
    border-color: {p.text_muted};
}}
QPushButton:pressed {{
    background-color: {p.bg1};
}}
QPushButton:disabled {{
    color: {p.text_dim};
    border-color: {p.border_muted};
}}

#PrimaryBtn {{
    background-color: {p.accent_alt};
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    color: {p.white};
    font-weight: 600;
}}
#PrimaryBtn:hover {{
    background-color: {p.accent};
}}
#PrimaryBtn:disabled {{
    background-color: {p.accent_muted};
    color: {p.text_muted};
}}

#DangerBtn {{
    background-color: transparent;
    border: 1px solid {p.danger};
    color: {p.danger};
    border-radius: 8px;
    padding: 8px 18px;
    font-weight: 500;
}}
#DangerBtn:hover {{
    background-color: {p.danger};
    color: {p.white};
}}

#SuccessBtn {{
    background-color: {p.projection};
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    color: {p.white};
    font-weight: 600;
}}
#SuccessBtn:hover {{
    background-color: {p.success_hover};
}}

/* === Player Controls === */
#PlayerCard {{
    background-color: {p.bg1};
    border: 1px solid {p.border};
    border-radius: 14px;
    padding: 16px;
}}

#PlayerTitle {{
    background: transparent;
    font-size: 15px;
    font-weight: 600;
    color: {p.text_primary};
}}
#PlayerSubtitle {{
    background: transparent;
    font-size: 12px;
    color: {p.text_muted};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {p.bg3};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {p.accent};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {p.white};
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {p.accent_alt};
    border-radius: 2px;
}}

/* === Status Bar === */
#StatusBar {{
    background-color: {p.bg1};
    border-top: 1px solid {p.border};
}}
#StatusLabel {{
    color: {p.text_muted};
    font-size: 11px;
}}
#StatusActive {{
    color: {p.success};
    font-size: 11px;
    font-weight: 600;
}}

/* === Settings === */
#SettingsGroup {{
    background: {p.bg1};
    border: 1px solid {p.border};
    border-radius: 12px;
    padding: 16px;
}}
#SettingsGroupTitle {{
    background: transparent;
    font-size: 11px;
    font-weight: 700;
    color: {p.text_muted};
    text-transform: uppercase;
    letter-spacing: 0.8px;
}}

#LangCard {{
    background: {p.bg2};
    border: 1px solid {p.border_muted};
    border-radius: 10px;
    padding: 10px 14px;
}}
#LangCard:hover {{
    border-color: {p.border};
    background: {p.bg3};
}}
#LangCard[selected="true"] {{
    border-color: {p.accent_alt};
    background: {p.accent_muted};
}}

#ScreenInfoCard {{
    background: {p.bg2};
    border: 1px solid {p.border_muted};
    border-radius: 10px;
    padding: 12px 16px;
}}

/* === Projection Badge === */
#ProjectionBadge {{
    background: {p.projection};
    border-radius: 6px;
    padding: 3px 8px;
    color: {p.white};
    font-size: 11px;
    font-weight: 700;
}}

/* === Loading Spinner Label === */
#LoadingLabel {{
    background: transparent;
    color: {p.text_muted};
    font-size: 13px;
}}

/* === Tooltip === */
QToolTip {{
    background-color: {p.bg3};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* === ComboBox === */
QComboBox {{
    background: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 7px 12px;
    color: {p.text_primary};
    selection-background-color: {p.accent_muted};
}}
QComboBox:hover {{ border-color: {p.accent_alt}; }}
QComboBox::drop-down {{ width: 24px; border: none; }}
QComboBox QAbstractItemView {{
    background: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 8px;
    selection-background-color: {p.accent_muted};
}}

/* === Menu === */
QMenu {{
    background: {p.bg2};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 20px;
    border-radius: 6px;
    color: {p.text_primary};
}}
QMenu::item:selected {{
    background: {p.accent_muted};
    color: {p.accent};
}}
"""


SCROLLBAR_STYLESHEET = scrollbar_stylesheet()
STYLESHEET = app_stylesheet()

__all__ = [
    "APP_THEME",
    "METRICS",
    "PALETTE",
    "QML_THEME",
    "SCROLLBAR_STYLESHEET",
    "STYLESHEET",
    "THEME",
    "AppTheme",
    "ThemeMetrics",
    "ThemePalette",
    "app_stylesheet",
    "qss_rgba",
    "scrollbar_stylesheet",
]
