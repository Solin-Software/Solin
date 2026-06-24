"""Theme manager and public theme tokens for Solin's UI.

Theme definitions live in ``solin.styles.themes``. This module keeps the
application-facing API stable and owns the active theme used while widgets are
constructed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Literal

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from solin.styles.themes.base import AppTheme, ThemeMetrics, ThemePalette
from solin.styles.themes.registry import (
    DEFAULT_THEME_ID,
    available_themes,
    get_theme,
    normalize_theme_id,
)


THEME = get_theme(DEFAULT_THEME_ID)
APP_THEME = THEME
SCROLLBAR_STYLESHEET = ""
STYLESHEET = ""


class _PaletteProxy:
    """Stable public palette object that always resolves the active theme."""

    def __getattr__(self, name: str) -> str:
        return getattr(THEME.palette, name)


class _MetricsProxy:
    """Stable public metrics object that always resolves the active theme."""

    def __getattr__(self, name: str) -> str:
        return getattr(THEME.metrics, name)


class _QmlThemeProxy(Mapping[str, object]):
    """Mapping facade used by QML hosts without freezing imported references."""

    def _data(self) -> dict[str, object]:
        return THEME.qml_palette()

    def __getitem__(self, key: str) -> object:
        return self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


PALETTE = _PaletteProxy()
METRICS = _MetricsProxy()
QML_THEME = _QmlThemeProxy()


def _theme_runtime(theme: AppTheme) -> tuple[AppTheme, str, str]:
    return (
        theme,
        scrollbar_stylesheet(theme),
        app_stylesheet(theme),
    )


def activate_theme(theme_id: str) -> AppTheme:
    """Activate a registered theme for widgets constructed after this call."""

    global APP_THEME, SCROLLBAR_STYLESHEET, STYLESHEET, THEME
    theme = get_theme(theme_id)
    THEME, SCROLLBAR_STYLESHEET, STYLESHEET = _theme_runtime(theme)
    APP_THEME = THEME
    return THEME


def current_theme() -> AppTheme:
    return THEME


def current_theme_scheme(theme: AppTheme | None = None) -> Literal["dark", "light"]:
    return "light" if (theme or THEME).id == "light" else "dark"


def qss_rgba(hex_color: str, alpha: float) -> str:
    """Return a Qt stylesheet rgba() color derived from a hex token."""

    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB color, got {hex_color!r}")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:g})"


def tooltip_stylesheet(theme: AppTheme | None = None) -> str:
    p = (theme or THEME).palette
    return f"""
QToolTip {{
    background-color: {p.surface_overlay};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}}
"""


def apply_application_palette(
    app: QApplication | None,
    theme: AppTheme | None = None,
) -> None:
    if app is None:
        return
    p = (theme or THEME).palette
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.surface_overlay))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text_primary))
    app.setPalette(palette)


def slider_handle_fill(theme: AppTheme | None = None) -> str:
    p = (theme or THEME).palette
    return p.text_secondary if current_theme_scheme(theme) == "dark" else p.accent


def slider_handle_border(theme: AppTheme | None = None) -> str:
    p = (theme or THEME).palette
    return "transparent" if current_theme_scheme(theme) == "dark" else p.white


def slider_stylesheet(
    *,
    theme: AppTheme | None = None,
    groove_height: int = 4,
    handle_size: int = 12,
    groove_color: str | None = None,
    progress_color: str | None = None,
    handle_fill: str | None = None,
    handle_hover: str | None = None,
    handle_border: str | None = None,
) -> str:
    p = (theme or THEME).palette
    handle_margin = -max(0, (handle_size - groove_height) // 2)
    handle_radius = handle_size // 2
    groove_radius = max(1, groove_height // 2)
    resolved_handle_fill = handle_fill or slider_handle_fill(theme)
    resolved_handle_hover = handle_hover or (
        p.text_primary if current_theme_scheme(theme) == "dark" else p.accent_text_hover
    )
    resolved_handle_border = handle_border or slider_handle_border(theme)
    border_rule = (
        "border:none;"
        if resolved_handle_border == "transparent"
        else f"border:1px solid {resolved_handle_border};"
    )
    resolved_progress = progress_color or (
        p.accent_hover if current_theme_scheme(theme) == "dark" else p.accent
    )
    return (
        "QSlider{background:transparent;border:none;}"
        f"QSlider::groove:horizontal{{height:{groove_height}px;"
        f"background:{groove_color or p.border_muted};"
        f"border-radius:{groove_radius}px;}}"
        f"QSlider::handle:horizontal{{width:{handle_size}px;height:{handle_size}px;"
        f"margin:{handle_margin}px 0;background:{resolved_handle_fill};"
        f"{border_rule}"
        f"border-radius:{handle_radius}px;}}"
        f"QSlider::handle:horizontal:hover{{background:{resolved_handle_hover};}}"
        f"QSlider::sub-page:horizontal{{background:{resolved_progress};"
        f"border-radius:{groove_radius}px;}}"
    )


def scrollbar_stylesheet(theme: AppTheme | None = None) -> str:
    p = (theme or THEME).palette
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


def app_stylesheet(theme: AppTheme | None = None) -> str:
    theme = theme or THEME
    p = theme.palette
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
{scrollbar_stylesheet(theme)}

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

#SidebarSeparator {{
    background: {p.border_muted};
    border: none;
    margin: 4px 0;
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
    background: {slider_handle_fill(theme)};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border: 1px solid {slider_handle_border(theme)};
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {p.accent_text_hover};
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
{tooltip_stylesheet(theme)}

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


activate_theme(DEFAULT_THEME_ID)

__all__ = [
    "APP_THEME",
    "DEFAULT_THEME_ID",
    "METRICS",
    "PALETTE",
    "QML_THEME",
    "SCROLLBAR_STYLESHEET",
    "STYLESHEET",
    "THEME",
    "AppTheme",
    "ThemeMetrics",
    "ThemePalette",
    "activate_theme",
    "apply_application_palette",
    "app_stylesheet",
    "available_themes",
    "current_theme",
    "current_theme_scheme",
    "get_theme",
    "normalize_theme_id",
    "qss_rgba",
    "scrollbar_stylesheet",
    "slider_handle_border",
    "slider_handle_fill",
    "slider_stylesheet",
    "tooltip_stylesheet",
]
