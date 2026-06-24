from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    """Semantic color tokens for Solin UI themes."""

    bg0: str
    bg1: str
    bg2: str
    bg3: str

    surface: str
    surface_card: str
    surface_alt: str
    surface_input_focus: str
    surface_hover: str
    surface_hover_strong: str
    surface_overlay: str
    media_placeholder: str

    border: str
    border_muted: str
    border_subtle: str
    border_strong: str

    text_primary: str
    text_secondary: str
    text_muted: str
    text_dim: str
    text_faint: str
    text_on_accent: str

    accent: str
    accent_alt: str
    accent_hover: str
    accent_pressed: str
    accent_selection: str
    accent_muted: str
    accent_muted_hover: str
    accent_tint: str
    accent_text: str
    accent_text_hover: str

    success: str
    success_surface: str
    success_surface_strong: str
    success_border: str
    success_hover: str
    success_pressed: str
    warning: str
    warning_text: str
    danger: str
    danger_text: str
    danger_surface: str
    danger_border: str
    danger_surface_hover: str
    danger_subtle: str

    projection: str
    titlebar: str
    black: str
    white: str

    def as_qml(self) -> dict[str, object]:
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
    id: str
    display_name: str
    palette: ThemePalette
    metrics: ThemeMetrics = ThemeMetrics()
    hover_opacity: float = 1.0
    hover_strong_opacity: float = 1.0
    section_hover_opacity: float = 0.9

    def qml_palette(self) -> dict[str, object]:
        palette = self.palette.as_qml()
        palette["themeId"] = self.id
        palette["isDark"] = self.id == "dark"
        palette["hoverOpacity"] = self.hover_opacity
        palette["hoverStrongOpacity"] = self.hover_strong_opacity
        palette["sectionHoverOpacity"] = self.section_hover_opacity
        return palette
